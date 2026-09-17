// Synthetic topics only. manage.py e2e owns and removes the temporary database.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createRequire } = require('node:module');
const projectRequire = createRequire(path.resolve(__dirname, '../web/package.json'));
const { chromium } = projectRequire('playwright');
const base = process.env.LEARNING_TREE_E2E_BASE;
if (process.env.LEARNING_TREE_E2E !== '1' || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base ?? '')) {
  throw new Error('Use: uv run python scripts/manage.py e2e');
}

// Check the launcher isolation before the first API mutation, not just the URL.
const isolated = spawnSync(process.env.LEARNING_TREE_E2E_PYTHON, ['-c', `
import os
from pathlib import Path
from sqlalchemy.engine import make_url
database = Path(make_url(os.environ['DATABASE_URL']).database).resolve()
assert os.environ['LEARNING_TREE_E2E'] == '1'
assert database.parent.name.startswith('learning-tree-e2e-')
assert database.name == 'test.db'
assert os.environ['DEFAULT_LLM_MODEL'] == 'mock'
assert os.environ['DEFAULT_API_KEY'] == 'mock'
`], { cwd: path.resolve(__dirname, '..'), encoding: 'utf8' });
assert.equal(isolated.status, 0, isolated.stderr);

async function api(route, body, method = 'POST') {
  const response = await fetch(base + '/api' + route, body === undefined ? {} : {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
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
  assert(models.length > 0 && models.every(model => model.llm_model === 'mock'), 'Only offline models are allowed');
  const originalTitle = 'Topic UI check · Compiler notes';
  const renamedTitle = 'Topic UI check · Parsing and syntax';
  const topic = await api('/trees/import', {
    format: 'branch-learning', version: 1, tree: { title: originalTitle },
    nodes: [
      { id: 1, parent_id: null, title: 'Original root question', kind: 'root', status: 'complete', learning_note: 'Keep this reflection unchanged.' },
      { id: 2, parent_id: 1, title: 'Original branch question', kind: 'branch', status: 'complete', seed_text: 'Parser example', source_node_id: 1, source_message_id: 2 },
    ],
    messages: [
      { id: 1, node_id: 1, role: 'user', content: 'How does a parser work?' },
      { id: 2, node_id: 1, role: 'assistant', content: 'Parser example: tokens become a syntax tree.', answered_by: 'Demo (offline)' },
      { id: 3, node_id: 2, role: 'user', content: 'Explain recursive descent.' },
      { id: 4, node_id: 2, role: 'assistant', content: 'Each parsing function handles one grammar rule.', answered_by: 'Demo (offline)' },
    ],
  });
  await api('/trees', { title: 'Topic UI check · Next active topic' });
  const originalNodes = await api(`/trees/${topic.id}`);
  const branch = originalNodes.find(node => node.parent_id !== null);
  const originalThread = await api(`/nodes/${branch.id}/thread`);
  const originalRoot = await api(`/nodes/${topic.root_node_id}`);
  const originalBackup = await api(`/trees/${topic.id}/export`);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [], checks = [];
  const artifacts = path.resolve(__dirname, '../artifacts');
  fs.mkdirSync(artifacts, { recursive: true });
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === base) return route.continue();
    errors.push('Unexpected external browser request');
    return route.abort();
  });
  await context.addInitScript(treeId => {
    localStorage.setItem('learning-tree.locale', 'en');
    localStorage.setItem('bl-theme', 'dark');
    if (!localStorage.getItem('bl-tree')) localStorage.setItem('bl-tree', JSON.stringify(treeId));
  }, topic.id);
  const sidebar = page.getByRole('complementary', { name: 'Learning topics', exact: true });
  const topicButton = title => sidebar.getByRole('button', { name: title, exact: true });
  const topicActions = title => sidebar.getByRole('button', { name: `Topic actions: ${title}`, exact: true });
  const heading = title => page.getByRole('heading', { name: title, exact: true, level: 1 });
  const menuItem = name => page.getByRole('menuitem', { name, exact: true });
  const savedTopic = async () => (await api('/trees?include_archived=true')).find(tree => tree.id === topic.id);
  async function openMenu(title) {
    await topicButton(title).hover();
    await topicActions(title).click();
    await page.getByRole('menu').waitFor();
  }
  async function openRename(title) {
    await openMenu(title);
    await menuItem('Rename').click();
    const dialog = page.getByRole('dialog', { name: 'Rename topic', exact: true });
    await dialog.waitFor();
    return dialog;
  }
  async function showArchived() {
    await sidebar.getByRole('button', { name: /^Archived(?:\s|$)/ }).click();
  }
  async function showActive() {
    await sidebar.getByRole('button', { name: 'Back to topics', exact: true }).click();
  }
  async function assertLearningUnchanged() {
    assert.deepEqual(await api(`/trees/${topic.id}`), originalNodes, 'Topic organization preserves every node');
    assert.deepEqual(await api(`/nodes/${topic.root_node_id}`), originalRoot, 'Root question, answers and reflection are unchanged');
    assert.deepEqual(await api(`/nodes/${branch.id}/thread`), originalThread, 'Branch and full learning path are unchanged');
  }
  try {
    await page.goto(base);
    await heading(originalTitle).waitFor();

    // Keyboard users can reach the overflow action and dismiss its menu.
    await topicActions(originalTitle).focus();
    await page.keyboard.press('Enter');
    await page.getByRole('menu').waitFor();
    await page.screenshot({ path: path.join(artifacts, 'topic-menu-desktop.png') });
    await page.keyboard.press('Escape');
    await page.getByRole('menu').waitFor({ state: 'hidden' });
    assert(await topicActions(originalTitle).evaluate(element => element === document.activeElement), 'Escape returns focus to the action button');

    let dialog = await openRename(originalTitle);
    const titleInput = dialog.getByRole('textbox', { name: 'Topic name', exact: true });
    await titleInput.fill('   ');
    assert(await dialog.getByRole('button', { name: 'Save', exact: true }).isDisabled(), 'Whitespace-only names cannot be saved');
    await titleInput.fill('Discard this draft');
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    assert.equal((await savedTopic()).title, originalTitle, 'Escape does not persist a rename');

    dialog = await openRename(originalTitle);
    await dialog.getByRole('textbox', { name: 'Topic name', exact: true }).fill(`  ${renamedTitle}  `);
    await dialog.getByRole('button', { name: 'Save', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    await heading(renamedTitle).waitFor();
    await topicButton(renamedTitle).waitFor();
    assert.equal((await savedTopic()).title, renamedTitle, 'Saved topic titles are trimmed');
    await assertLearningUnchanged();
    await page.reload();
    await heading(renamedTitle).waitFor();
    checks.push('Keyboard and Escape work; rename rejects blanks, trims names, updates both headings and persists without changing learning content');

    // Fail only this topic's PATCH request; keep reads and other topics real.
    const routePattern = `**/api/trees/${topic.id}`;
    let failures = 0;
    const rejectPatch = async route => {
      if (route.request().method() !== 'PATCH') return route.fallback();
      failures += 1;
      await route.fulfill({ status: 503, json: { detail: 'Synthetic topic update unavailable.' } });
    };
    await page.route(routePattern, rejectPatch);
    dialog = await openRename(renamedTitle);
    await dialog.getByRole('textbox', { name: 'Topic name', exact: true }).fill('Failed title must stay a draft');
    await dialog.getByRole('button', { name: 'Save', exact: true }).click();
    await poll(async () => (await dialog.innerText()).includes('Synthetic topic update unavailable.'), 'rename failure is visible');
    assert.equal(await dialog.getByRole('textbox', { name: 'Topic name', exact: true }).inputValue(), 'Failed title must stay a draft');
    assert.equal((await savedTopic()).title, renamedTitle);
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    await openMenu(renamedTitle);
    await menuItem('Archive').click();
    await poll(async () => failures === 2 && (await page.getByRole('menu').innerText()).includes('Synthetic topic update unavailable.'), 'archive failure is visible at its action menu');
    await heading(renamedTitle).waitFor();
    assert.equal((await savedTopic()).archived, false, 'A failed archive cannot remove the topic');
    assert.equal(await topicButton(renamedTitle).getAttribute('aria-current'), 'page');
    await page.unroute(routePattern, rejectPatch);
    // Some menu implementations retain the menu on error; dismiss it explicitly.
    await page.keyboard.press('Escape');
    checks.push('Targeted server errors preserve the selected topic, saved title and rename draft');

    await openMenu(renamedTitle);
    await menuItem('Archive').click();
    await poll(async () => (await savedTopic()).archived === true, 'archive persisted');
    await topicButton(renamedTitle).waitFor({ state: 'hidden' });
    await poll(async () => await heading(renamedTitle).count() === 0 && await page.locator('h1').count() === 1, 'archive selects another active topic');
    assert(!(await api('/trees')).some(tree => tree.id === topic.id), 'The default API list excludes archived topics');
    await page.reload();
    await sidebar.waitFor();
    await showArchived();
    await topicButton(renamedTitle).click();
    await heading(renamedTitle).waitFor();
    assert.equal((await savedTopic()).archived, true, 'Opening an archived topic does not restore it');
    await page.screenshot({ path: path.join(artifacts, 'topic-archive-desktop.png') });
    await openMenu(renamedTitle);
    await menuItem('Restore').click();
    await poll(async () => (await savedTopic()).archived === false, 'restore persisted');
    if (await sidebar.getByRole('button', { name: 'Back to topics', exact: true }).count()) await showActive();
    await topicButton(renamedTitle).click();
    await heading(renamedTitle).waitFor();
    await assertLearningUnchanged();
    checks.push('Archive survives reload, opens readably without restoring, and restores with the same messages and branches');

    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: 'Open topic list', exact: true }).click();
    await openMenu(renamedTitle);
    const menuBounds = await page.getByRole('menu').boundingBox();
    assert(menuBounds && menuBounds.x >= 0 && menuBounds.x + menuBounds.width <= 391, 'Topic menu fits a narrow viewport');
    await page.screenshot({ path: path.join(artifacts, 'topic-menu-mobile.png') });
    await menuItem('Rename').click();
    dialog = page.getByRole('dialog', { name: 'Rename topic', exact: true });
    const dialogBounds = await dialog.boundingBox();
    assert(dialogBounds && dialogBounds.x >= 0 && dialogBounds.x + dialogBounds.width <= 391, 'Rename dialog fits a narrow viewport');
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    await page.setViewportSize({ width: 1440, height: 1000 });
    checks.push('The topic action menu and rename dialog fit mobile screens');

    // Keep the browser's real offline-model response stream open after receiving
    // its chunks. This makes the archive/Stop race deterministic without sleeps,
    // an external model or fake topic mutations. Abort still errors the reader.
    const streamingTitle = 'Topic UI check · Streaming archive';
    const streamingTopic = await api('/trees', { title: streamingTitle });
    await page.evaluate(id => localStorage.setItem('bl-tree', JSON.stringify(id)), streamingTopic.id);
    await page.reload();
    await heading(streamingTitle).waitFor();
    await page.evaluate(nodeId => {
      const originalFetch = window.fetch.bind(window);
      let held = false;
      window.fetch = async (input, init) => {
        const response = await originalFetch(input, init);
        const requestUrl = input instanceof Request ? input.url : String(input);
        if (held || new URL(requestUrl, location.href).pathname !== `/api/nodes/${nodeId}/ask` || init?.method !== 'POST') return response;
        held = true;
        const reader = response.body.getReader();
        const body = new ReadableStream({
          start(controller) {
            init.signal.addEventListener('abort', () => {
              void reader.cancel();
              controller.error(new DOMException('Synthetic transport stopped', 'AbortError'));
            }, { once: true });
            void (async () => {
              try {
                for (;;) {
                  const { done, value } = await reader.read();
                  if (done) return; // Hold EOF until the app's Stop action aborts.
                  controller.enqueue(value);
                }
              } catch (error) { controller.error(error); }
            })();
          },
          cancel() { return reader.cancel(); },
        });
        return new Response(body, { status: response.status, headers: response.headers });
      };
    }, streamingTopic.root_node_id);
    const composer = page.getByRole('textbox', { name: 'Enter a question', exact: true });
    await composer.fill('Explain this synthetic archive regression in one sentence.');
    await composer.press('Enter');
    const stop = page.getByRole('button', { name: 'Stop response', exact: true });
    await stop.waitFor();
    await poll(async () => (await api(`/nodes/${streamingTopic.root_node_id}`)).messages.some(message => message.role === 'assistant'), 'offline-model response persisted');
    await openMenu(streamingTitle);
    await menuItem('Archive').click();
    await poll(async () => (await api('/trees?include_archived=true')).find(tree => tree.id === streamingTopic.id)?.archived === true, 'streaming topic archived');
    await heading(streamingTitle).waitFor();
    assert(await stop.isVisible(), 'Archiving an in-flight conversation keeps Stop reachable');
    assert.equal(await topicButton(streamingTitle).getAttribute('aria-current'), 'page');
    await stop.click();
    await stop.waitFor({ state: 'hidden' });
    await page.reload(); // Restore native fetch and verify archived selection remains readable.
    await heading(streamingTitle).waitFor();
    await showActive();
    await topicButton(renamedTitle).click();
    await heading(renamedTitle).waitFor();
    checks.push('Archiving during an open response stream keeps its chat and Stop control available; Stop closes the held stream');

    // Exercise the existing recoverable-delete path through the new menu.
    await openMenu(renamedTitle);
    page.once('dialog', confirmation => confirmation.accept());
    await menuItem('Delete').click();
    await poll(async () => !(await api('/trees?include_archived=true')).some(tree => tree.id === topic.id), 'delete completed');
    const recovery = await page.evaluate(() => JSON.parse(localStorage.getItem('bl-recovery')));
    assert.equal(recovery.tree.title, renamedTitle);
    assert.deepEqual(recovery.nodes, originalBackup.nodes);
    assert.deepEqual(recovery.messages, originalBackup.messages);
    await sidebar.getByRole('button', { name: '↶ Restore last deletion', exact: true }).click();
    await heading(renamedTitle).waitFor();
    const recovered = (await api('/trees')).find(tree => tree.title === renamedTitle);
    assert(recovered && recovered.id !== topic.id && !recovered.archived, 'Recovery creates a new active topic');
    const restoredBackup = await api(`/trees/${recovered.id}/export`);
    assert.deepEqual(restoredBackup.messages.map(message => [message.role, message.content]), originalBackup.messages.map(message => [message.role, message.content]));
    assert.deepEqual(restoredBackup.nodes.map(node => node.title), originalBackup.nodes.map(node => node.title));
    checks.push('Delete from the menu retains a full backup; sidebar recovery restores messages and branches as a new active topic');

    // With no next active topic, archiving safely clears the conversation pane.
    for (const other of await api('/trees')) {
      if (other.id !== recovered.id) await api(`/trees/${other.id}`, { archived: true }, 'PATCH');
    }
    await page.reload();
    await heading(renamedTitle).waitFor();
    await openMenu(renamedTitle);
    await menuItem('Archive').click();
    await poll(async () => (await api('/trees')).length === 0, 'last active topic archived');
    await heading('Learning space').waitFor();
    await page.getByRole('heading', { name: 'Start with curiosity', exact: true }).waitFor();
    await showArchived();
    await topicButton(renamedTitle).click();
    await heading(renamedTitle).waitFor();
    checks.push('Archiving the last active topic leaves a clean empty chat and the archived conversation remains accessible');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, checks }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
