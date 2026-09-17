// Isolated, imported conversations only; no model or tool calls are needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createRequire } = require('node:module');
const { chromium } = createRequire(path.resolve(__dirname, '../web/package.json'))('playwright');
const base = process.env.LEARNING_TREE_E2E_BASE;
if (process.env.LEARNING_TREE_E2E !== '1' || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base ?? '')) {
  throw new Error('Use: uv run python scripts/manage.py e2e');
}
const isolated = spawnSync(process.env.LEARNING_TREE_E2E_PYTHON, ['-c', `
import os
from pathlib import Path
from sqlalchemy.engine import make_url
database = Path(make_url(os.environ['DATABASE_URL']).database).resolve()
assert os.environ['LEARNING_TREE_E2E'] == '1'
assert database.parent.name.startswith('learning-tree-e2e-') and database.name == 'test.db'
assert os.environ['DEFAULT_LLM_MODEL'] == 'mock' and os.environ['DEFAULT_API_KEY'] == 'mock'
`], { cwd: path.resolve(__dirname, '..'), encoding: 'utf8' });
assert.equal(isolated.status, 0, isolated.stderr);

async function api(route, body) {
  const response = await fetch(`${base}/api${route}`, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  assert(response.ok, `${route}: ${response.status}`);
  return response.json();
}
async function poll(callback, label) {
  for (let index = 0; index < 120; index++) {
    if (await callback()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${label}`);
}

(async () => {
  const models = await api('/models');
  assert(models.length > 0 && models.every(model => model.llm_model === 'mock'));
  const questions = Array.from({ length: 20 }, (_, index) => `Question ${String(index + 1).padStart(2, '0')}: Parser checkpoint ${index + 1}`);
  const nodes = Array.from({ length: 19 }, (_, index) => ({
    id: index + 1, parent_id: index === 0 ? null : index,
    title: questions[index === 0 ? 0 : index + 1],
    kind: index === 0 ? 'root' : 'followup', status: 'complete',
  }));
  const messages = questions.flatMap((question, index) => {
    const node_id = index < 2 ? 1 : index;
    const answer = [
      `Answer ${index + 1}. Selected wording: **ORCHID-417** and syntax trees.`,
      ...Array.from({ length: 7 }, (_, paragraph) => `Paragraph ${paragraph + 1}: A parser reads tokens in order and checks them against a grammar. Each production describes a small, testable piece of the language. Keep the current input position and return a syntax node when the rule succeeds. This synthetic explanation creates a long conversation for navigation checks.`),
    ].join('\n\n');
    return [
      { id: index * 2 + 1, node_id, role: 'user', content: question },
      { id: index * 2 + 2, node_id, role: 'assistant', content: answer, answered_by: 'Demo (offline)' },
    ];
  });
  const tree = await api('/trees/import', { format: 'branch-learning', version: 1, tree: { title: 'Chat navigation UI check' }, nodes, messages });
  const storedNodes = await api(`/trees/${tree.id}`);
  const leaf = storedNodes.find(node => !storedNodes.some(other => other.parent_id === node.id));
  const thread = (await api(`/nodes/${leaf.id}/thread`)).nodes;
  assert.equal(thread.length, 20);
  assert.equal(thread[0].node_id, thread[1].node_id, 'The fixture includes multiple legacy question/answer pairs on one node');
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base });
  const page = await context.newPage();
  const errors = [], checks = [];
  const artifacts = path.resolve(__dirname, '../artifacts');
  fs.mkdirSync(artifacts, { recursive: true });
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === base) return route.continue();
    errors.push('Unexpected external request');
    return route.abort();
  });
  await context.addInitScript(({ treeId, leafId }) => {
    localStorage.setItem('learning-tree.locale', 'en');
    localStorage.setItem('bl-theme', 'dark');
    localStorage.setItem('bl-tree', JSON.stringify(treeId));
    localStorage.setItem(`bl-node:${treeId}`, JSON.stringify(leafId));
  }, { treeId: tree.id, leafId: leaf.id });
  const rail = page.getByRole('navigation', { name: 'Question navigation', exact: true });
  const tick = number => rail.getByRole('button', { name: `Jump to question ${number}: ${questions[number - 1]}`, exact: true });
  const turn = number => page.locator(`[data-turn-key="${thread[number - 1].node_id}:${thread[number - 1].question_message_id}"]`);
  const scroller = page.locator('.chat-scroll');
  async function assertQuestionVisible(number) {
    await poll(async () => turn(number).locator('.chat-question').evaluate(element => {
      const rect = element.getBoundingClientRect();
      const viewport = element.closest('.chat-scroll').getBoundingClientRect();
      return rect.top >= viewport.top - 4 && rect.top < viewport.bottom - 20;
    }), `question ${number} is visible in its own turn`);
  }
  try {
    await page.goto(base);
    await page.getByRole('heading', { name: 'Chat navigation UI check', exact: true, level: 1 }).waitFor();
    await rail.waitFor();
    await poll(async () => await rail.getByRole('button').count() === 20, 'all question ticks rendered');
    const keys = await page.locator('.chat-turn[data-turn-key]').evaluateAll(elements => elements.map(element => element.dataset.turnKey));
    assert.equal(keys.length, 20);
    assert.equal(new Set(keys).size, 20, 'Legacy pairs sharing one node still have distinct turn identities');
    let pathReads = 0, modelCalls = 0, branchCalls = 0;
    page.on('request', request => {
      const pathname = new URL(request.url()).pathname;
      if (/\/api\/nodes\/\d+\/thread$/.test(pathname)) pathReads += 1;
      if (/\/api\/nodes\/\d+\/(?:ask|explain)$/.test(pathname)) modelCalls += 1;
      if (/\/api\/nodes\/\d+\/branch$/.test(pathname)) branchCalls += 1;
    });
    const selectedNode = await page.evaluate(treeId => localStorage.getItem(`bl-node:${treeId}`), tree.id);
    await tick(5).hover();
    await page.getByRole('tooltip').filter({ hasText: questions[4] }).waitFor();
    await page.screenshot({ path: path.join(artifacts, 'chat-question-navigation-desktop.png') });
    await tick(5).click();
    await assertQuestionVisible(5);
    await poll(async () => await tick(5).getAttribute('aria-current') === 'step', 'active tick follows jump');
    await tick(2).focus();
    await page.getByRole('tooltip').filter({ hasText: questions[1] }).waitFor();
    await page.keyboard.press('Enter');
    await assertQuestionVisible(2);
    await poll(async () => await tick(2).getAttribute('aria-current') === 'step', 'legacy second pair is active');
    assert.equal(await page.evaluate(treeId => localStorage.getItem(`bl-node:${treeId}`), tree.id), selectedNode, 'Question jumps do not change the active learning path');
    checks.push('Twenty questions have distinct navigation ticks, including legacy pairs sharing a node; hover and keyboard preview, then jump within the current chat');

    await turn(12).evaluate(element => {
      const scroller = element.closest('.chat-scroll');
      scroller.scrollTop += element.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 12;
    });
    await assertQuestionVisible(12);
    await poll(async () => await tick(12).getAttribute('aria-current') === 'step', 'active tick tracks ordinary scrolling');
    await tick(2).click();
    await assertQuestionVisible(2);
    const selectedText = 'ORCHID-417 and syntax trees';
    const answer = turn(2).locator('[data-answer-node]');
    await answer.locator('p').first().scrollIntoViewIfNeeded();
    await answer.evaluate((element, text) => {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      const start = element.textContent.indexOf(text);
      if (start < 0) throw new Error('Synthetic selection text missing');
      const end = start + text.length;
      const range = document.createRange();
      let offset = 0, started = false, current;
      while ((current = walker.nextNode())) {
        const length = current.textContent.length;
        if (!started && start < offset + length) { range.setStart(current, start - offset); started = true; }
        if (started && end <= offset + length) { range.setEnd(current, end - offset); break; }
        offset += length;
      }
      const selection = window.getSelection();
      selection.removeAllRanges(); selection.addRange(range);
      element.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
    }, selectedText);
    const copy = page.getByRole('button', { name: 'Copy selected text', exact: true });
    await copy.waitFor();
    await page.screenshot({ path: path.join(artifacts, 'chat-selection-copy.png') });
    await copy.click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), selectedText, 'Selection copy writes only selected plain text, including across Markdown nodes');
    await page.keyboard.press('Escape');
    assert.equal(modelCalls, 0, 'Navigation and copy never call a model');
    assert.equal(branchCalls, 0, 'Navigation and copy never create branches');
    assert.equal(pathReads, 0, 'Question navigation scrolls without reloading ancestor conversations');
    assert.equal(await page.locator('.chat-turn[data-turn-key]').count(), 20);
    checks.push('The active tick follows scrolling; copying a selected Markdown passage writes exact plain text without explanation, branch or model calls');

    await page.setViewportSize({ width: 390, height: 844 });
    await rail.waitFor();
    await tick(8).click();
    await assertQuestionVisible(8);
    const bounds = await rail.boundingBox();
    assert(bounds && bounds.x >= 0 && bounds.x + bounds.width <= 391, 'Navigation fits a narrow viewport');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert((await scroller.evaluate(element => element.scrollHeight)) > (await scroller.evaluate(element => element.clientHeight)));
    await page.screenshot({ path: path.join(artifacts, 'chat-question-navigation-mobile.png') });
    checks.push('The navigation rail remains usable on a narrow screen without horizontal overflow');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, checks }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
