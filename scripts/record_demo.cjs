// Internal recorder. The Python wrapper owns the temporary database and server.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const projectRequire = createRequire(path.resolve(__dirname, '../web/package.json'));
const { chromium } = projectRequire('playwright');
const base = process.env.LEARNING_TREE_DEMO_BASE;
const output = process.env.LEARNING_TREE_DEMO_OUTPUT;
if (process.env.LEARNING_TREE_DEMO !== '1' || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base ?? '') || !output) {
  throw new Error('Use: uv run python scripts/record_demo.py');
}
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
async function api(route, body) {
  const response = await fetch(base + '/api' + route, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  assert(response.ok, `Local API ${route}: ${response.status}`);
  return response.json();
}
async function poll(callback) {
  for (let attempt = 0; attempt < 150; attempt++) {
    const value = await callback();
    if (value) return value;
    await pause(100);
  }
  throw new Error('Timed out waiting for the isolated demo application');
}
(async () => {
  assert.equal((await api('/models')).length, 1);
  assert.equal((await api('/models'))[0].llm_model, 'mock');
  assert.deepEqual(await api('/trees'), []);
  assert.deepEqual(await api('/mcp'), []);
  const tree = await api('/trees', { title: 'How models learn' });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    recordVideo: { dir: output, size: { width: 1280, height: 800 } },
    colorScheme: 'light',
    reducedMotion: 'reduce',
  });
  const errors = [], checks = [];
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === base) return route.continue();
    errors.push('Unexpected external browser request');
    return route.abort();
  });
  await context.addInitScript(({ treeId }) => {
    localStorage.setItem('learning-tree.locale', 'en');
    localStorage.setItem('bl-tree', JSON.stringify(treeId));
    localStorage.setItem('bl-map-open', 'true');
  }, { treeId: tree.id });
  const page = await context.newPage();
  const video = page.video();
  const created = Date.now();
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(base);
    await page.getByRole('heading', { name: tree.title, exact: true, level: 1 }).waitFor();
    // This is a recording annotation, not an application feature. Reserve its
    // own space so it never hides the app or falsely implies live AI output.
    await page.addStyleTag({ content: `
      body { padding-top: 48px !important; box-sizing: border-box; }
      .app { height: calc(100dvh - 48px) !important; }
      #demo-caption { position: fixed; inset: 0 0 auto; height: 48px; z-index: 9999;
        display: flex; justify-content: space-between; align-items: center; padding: 0 23px;
        box-sizing: border-box; background: #203b36; color: #fff;
        font: 600 15px system-ui, sans-serif; letter-spacing: .1px; }
      #demo-caption small { font-size: 12px; font-weight: 400; color: #cfe2da; }
    ` });
    await page.evaluate(() => {
      const banner = document.createElement('div');
      banner.id = 'demo-caption';
      banner.innerHTML = '<span id="demo-step"></span><small>Offline demo · scripted answers</small>';
      document.body.append(banner);
    });
    const caption = async value => page.locator('#demo-step').evaluate((el, text) => { el.textContent = text; }, value);
    const frame = async name => page.screenshot({ path: path.join(output, `frame-${name}.png`) });
    const input = page.getByRole('textbox', { name: 'Enter a question', exact: true });
    const question = process.env.LEARNING_TREE_DEMO_QUESTION;
    const branchQuestion = process.env.LEARNING_TREE_DEMO_BRANCH_QUESTION;
    const started = Date.now();
    const start = (started - created) / 1000;
    await caption('1 / 5  Ask a question');
    await input.pressSequentially(question, { delay: 22 });
    await pause(650);
    await input.press('Enter');
    await poll(async () => (await api(`/nodes/${tree.root_node_id}`)).status === 'complete');
    const answer = page.locator(`[data-answer-node="${tree.root_node_id}"]`);
    await answer.waitFor();
    await pause(2800);
    await frame('01-question');
    checks.push('Question submitted through real UI; scripted answer persisted in temporary SQLite');

    await caption('2 / 5  Select an unfamiliar idea');
    const term = answer.locator('strong').filter({ hasText: /^validation set$/ });
    await term.scrollIntoViewIfNeeded();
    const box = await term.boundingBox();
    assert(box);
    await page.mouse.move(box.x + 1, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width - 1, box.y + box.height / 2, { steps: 18 });
    await page.mouse.up();
    const selection = page.getByRole('dialog', { name: 'Explain selected text' });
    await selection.waitFor();
    await pause(850);
    await selection.getByRole('button', { name: 'Explain', exact: true }).click();
    await selection.locator('.chat-selection-content').getByText(/practice check/).waitFor();
    await pause(3800);
    await frame('02-inline');

    await caption('3 / 5  Explore in a focused branch');
    await selection.getByRole('button', { name: 'New branch ↗', exact: true }).click();
    const branch = await poll(async () => (await api(`/trees/${tree.id}`)).find(node => node.kind === 'branch'));
    assert.equal(branch.seed_text, 'validation set');
    assert.equal(branch.source_node_id, tree.root_node_id);
    assert(branch.source_message_id && branch.source_end > branch.source_start);
    await page.locator('.chat-source-bar').waitFor();
    await poll(async () => !(await input.isDisabled()) && (await input.getAttribute('placeholder')).includes('validation set'));
    await input.pressSequentially(branchQuestion, { delay: 28 });
    await pause(450);
    await input.press('Enter');
    await poll(async () => (await api(`/nodes/${branch.id}`)).status === 'complete');
    await page.locator(`[data-answer-node="${branch.id}"]`).waitFor();
    await pause(3000);
    await frame('03-branch');
    checks.push('Selected text opens inline explanation and creates a real branch with saved source offsets');

    await caption('4 / 5  Put the idea in your own words');
    await page.getByRole('button', { name: 'My reflection', exact: true }).click();
    const reflection = 'Validation helps me compare choices; the final test stays untouched.';
    await page.getByRole('textbox', { name: 'My reflection', exact: true }).pressSequentially(reflection, { delay: 24 });
    await pause(2000);
    await frame('04-reflection');
    await page.getByRole('button', { name: 'Save reflection', exact: true }).click();
    await poll(async () => (await api(`/nodes/${branch.id}`)).learning_note === reflection);
    await pause(2500);
    checks.push('Personal reflection saved through real UI and read back from temporary SQLite');

    await caption('5 / 5  Return to the source with your reflection');
    await page.getByRole('button', { name: 'Continue with reflection', exact: true }).click();
    await poll(async () => (await input.inputValue()).includes(reflection));
    await answer.waitFor();
    assert.equal((await api(`/nodes/${tree.root_node_id}`)).messages.filter(message => message.role === 'user').length, 1,
      'Returning with reflection prepares an editable draft; it must not send automatically');
    await pause(1400);
    await frame('05-source');
    await pause(4500);
    assert.equal(await page.locator('.lm-card').count(), 2);
    checks.push('Back-to-source navigation restores the exact passage and prepares an unsent editable reflection draft');
    assert.deepEqual(errors, []);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    checks.push('No browser errors, external requests or horizontal overflow');
    const duration = (Date.now() - started) / 1000;
    await context.close();
    const file = await video.path();
    fs.writeFileSync(path.join(output, 'recording.json'), JSON.stringify({
      video: path.basename(file), start, duration, checks,
    }, null, 2));
  } finally {
    await context.close();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
