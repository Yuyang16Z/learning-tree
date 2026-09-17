// Synthetic imported trees only; manage.py owns and removes the isolated test database.
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
assert database.parent.name.startswith('learning-tree-e2e-') and database.name == 'test.db'
assert os.environ['DEFAULT_API_KEY'] == 'mock' and os.environ['DEFAULT_LLM_MODEL'] == 'mock'
`], { cwd: path.resolve(__dirname, '..'), encoding: 'utf8' });
assert.equal(isolated.status, 0, isolated.stderr);

async function api(route, body) {
  const response = await fetch(base + '/api' + route, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  assert(response.ok, `${route}: ${response.status} ${await response.clone().text()}`);
  return response.json();
}
async function poll(callback, label) {
  for (let index = 0; index < 100; index++) {
    if (await callback()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${label}`);
}

(async () => {
  const models = await api('/models');
  assert(models.length && models.every(model => model.llm_model === 'mock'), 'Only offline models are permitted');
  const titles = {
    topic: 'Node actions check · Failed learning attempt', root: 'Why do compilers use trees?',
    failed: 'Failed attempt about parsing', child: 'Child of the failed attempt',
    revision: 'Successful revised parsing question', grouped: 'A separate grouped branch', turn: 'Second turn in the grouped branch',
  };
  const topic = await api('/trees/import', {
    format: 'branch-learning', version: 1, tree: { title: titles.topic },
    nodes: [
      { id: 1, parent_id: null, title: titles.root, kind: 'root', status: 'complete' },
      { id: 2, parent_id: 1, title: titles.failed, kind: 'branch', status: 'error', error: 'Synthetic provider failure', seed_text: 'Parse a sentence', source_node_id: 1, source_message_id: 2 },
      { id: 3, parent_id: 2, title: titles.child, kind: 'branch', status: 'complete', seed_text: 'An unfinished example', source_node_id: 2, source_message_id: 5 },
      { id: 4, parent_id: 1, title: titles.revision, kind: 'revision', status: 'complete', revision_of: 2 },
      { id: 5, parent_id: 1, title: titles.grouped, kind: 'branch', status: 'complete', seed_text: 'A different topic', source_node_id: 1, source_message_id: 2 },
      { id: 6, parent_id: 5, title: titles.turn, kind: 'followup', status: 'complete' },
    ],
    messages: [
      { id: 1, node_id: 1, role: 'user', content: 'Why do compilers use trees?' },
      { id: 2, node_id: 1, role: 'assistant', content: 'Parse a sentence into a tree. A different topic can branch from here.' },
      { id: 3, node_id: 2, role: 'user', content: 'Failed parsing question with two stored attempts.' },
      { id: 4, node_id: 2, role: 'assistant', content: 'First failed partial answer.', status: 'error' },
      { id: 5, node_id: 2, role: 'assistant', content: 'An unfinished example from another failed attempt.', status: 'error' },
      { id: 6, node_id: 3, role: 'user', content: 'Child question belonging to the failed branch.' },
      { id: 7, node_id: 3, role: 'assistant', content: 'Child answer that should be deleted with its parent.' },
      { id: 8, node_id: 4, role: 'user', content: 'A successful revised parsing question.' },
      { id: 9, node_id: 4, role: 'assistant', content: 'Successful revision answer must remain intact.' },
      { id: 10, node_id: 5, role: 'user', content: 'Separate branch first question.' },
      { id: 11, node_id: 5, role: 'assistant', content: 'Separate branch first answer.' },
      { id: 12, node_id: 6, role: 'user', content: 'Separate branch follow-up question.' },
      { id: 13, node_id: 6, role: 'assistant', content: 'Separate branch follow-up answer.' },
    ],
  });
  const originalNodes = await api(`/trees/${topic.id}`);
  const byTitle = title => originalNodes.find(node => node.title === title);
  const failed = byTitle(titles.failed), child = byTitle(titles.child), revision = byTitle(titles.revision);
  const grouped = byTitle(titles.grouped), turn = byTitle(titles.turn);
  assert(failed && child && revision && grouped && turn, 'Imported fixture has all independently remapped nodes');
  const revisionBefore = await api(`/nodes/${revision.id}`);
  const rootBefore = await api(`/nodes/${topic.root_node_id}`);
  const groupedBefore = await api(`/nodes/${grouped.id}`);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [], deletes = [], checks = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (request.method() === 'DELETE') deletes.push(new URL(request.url()).pathname); });
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === base) return route.continue();
    errors.push('Unexpected external browser request');
    return route.abort();
  });
  await context.addInitScript(({ treeId, nodeId }) => {
    localStorage.setItem('learning-tree.locale', 'en');
    localStorage.setItem('bl-theme', 'dark');
    localStorage.setItem('bl-map-open', 'true');
    if (!localStorage.getItem('node-deletion-seeded')) {
      localStorage.setItem('bl-tree', JSON.stringify(treeId));
      localStorage.setItem(`bl-node:${treeId}`, JSON.stringify(nodeId));
      localStorage.setItem('node-deletion-seeded', '1');
    }
  }, { treeId: topic.id, nodeId: failed.id });
  const map = page.getByRole('complementary', { name: 'LearningTree', exact: true });
  const card = title => map.locator(`.lm-card[aria-label=${JSON.stringify(title)}]`);
  const menu = page.getByRole('menu', { name: 'Node actions', exact: true });
  const cardTrigger = title => card(title).locator('.lm-card-meta').getByRole('button', { name: `Node actions: ${title}`, exact: true });
  async function openCardMenu(title) {
    await cardTrigger(title).click();
    await menu.waitFor();
  }
  async function confirmAction(label, accept) {
    const awaited = page.waitForEvent('dialog');
    const clicked = menu.getByRole('menuitem', { name: label, exact: true }).click();
    const dialog = await awaited;
    const message = dialog.message();
    if (accept) await dialog.accept(); else await dialog.dismiss();
    await clicked;
    return message;
  }
  async function assertMenuInsideViewport() {
    const bounds = await menu.boundingBox();
    const viewport = page.viewportSize();
    assert(bounds && bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= viewport.width + 1 && bounds.y + bounds.height <= viewport.height + 1, 'Portal action menu stays within the viewport');
  }
  const artifacts = path.resolve(__dirname, '../artifacts');
  fs.mkdirSync(artifacts, { recursive: true });
  try {
    await page.goto(base);
    await page.getByRole('heading', { level: 1, name: titles.topic, exact: true }).waitFor();
    await card(titles.failed).waitFor();
    assert(await card(titles.failed).getByLabel('A response needs retrying', { exact: true }).count(), 'Failed node is shown in the learning map');

    await cardTrigger(titles.failed).focus();
    await page.keyboard.press('Enter');
    await menu.waitFor();
    await menu.getByRole('menuitem', { name: 'Delete branch', exact: true }).waitFor();
    await assertMenuInsideViewport();
    await page.keyboard.press('Escape');
    await menu.waitFor({ state: 'hidden' });
    assert(await cardTrigger(titles.failed).evaluate(element => element === document.activeElement), 'Escape restores focus to the failed-node actions');
    await openCardMenu(titles.failed);
    const cancelMessage = await confirmAction('Delete branch', false);
    assert(cancelMessage.includes(titles.failed) && /cannot be undone/i.test(cancelMessage), 'Confirmation names the failed node and permanence');
    assert(/1/.test(cancelMessage), 'Confirmation includes the one descendant being removed');
    assert.deepEqual(deletes, [], 'Cancelling must never send DELETE');
    assert.equal((await api(`/trees/${topic.id}`)).length, originalNodes.length);
    checks.push('Failed nodes expose a keyboard-accessible delete action; cancellation preserves all attempts and descendants');

    await map.getByRole('button', { name: 'Zoom out learning tree', exact: true }).click();
    await map.getByRole('button', { name: 'Zoom out learning tree', exact: true }).click();
    await openCardMenu(titles.failed);
    await assertMenuInsideViewport();
    await page.screenshot({ path: path.join(artifacts, 'node-actions-zoomed.png') });
    await page.keyboard.press('Escape');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: 'Show learning tree', exact: true }).click();
    await card(titles.failed).waitFor();
    await openCardMenu(titles.failed);
    await assertMenuInsideViewport();
    await page.screenshot({ path: path.join(artifacts, 'node-actions-mobile.png') });
    await page.keyboard.press('Escape');
    await page.setViewportSize({ width: 1440, height: 1000 });
    await map.waitFor();
    checks.push('Node menus remain usable and inside the viewport after zooming and on a narrow screen');

    await map.getByRole('button', { name: 'View the whole learning tree', exact: true }).click();
    await openCardMenu(titles.root);
    await menu.getByRole('menuitem', { name: 'Delete topic', exact: true }).waitFor();
    const rootMessage = await confirmAction('Delete topic', false);
    assert(rootMessage.includes(titles.topic) && /cannot be undone/i.test(rootMessage));
    assert.deepEqual(deletes, [], 'Cancelling root deletion preserves the complete topic');

    await card(titles.grouped).getByRole('button', { name: `Expand 2 turns in ${titles.grouped}`, exact: true }).click();
    const turnRow = card(titles.grouped).locator('.lm-round-row').filter({ has: page.getByRole('button', { name: `Node actions: ${titles.turn}`, exact: true }) });
    await turnRow.getByRole('button', { name: `Node actions: ${titles.turn}`, exact: true }).click();
    const roundMessage = await confirmAction('Delete branch', false);
    assert(roundMessage.includes(titles.turn), 'Expanded turns target their individual node rather than the group head');
    assert.deepEqual(deletes, []);
    checks.push('Root deletion clearly targets the topic, while expanded grouped turns target their own node');

    // Return to the failed node before deleting, so parent navigation is observable.
    await card(titles.failed).locator('.lm-topic').click();
    await poll(async () => await page.evaluate(({ treeId, nodeId }) => Number(localStorage.getItem(`bl-node:${treeId}`)) === nodeId, { treeId: topic.id, nodeId: failed.id }), 'failed node selected');
    await openCardMenu(titles.failed);
    await confirmAction('Delete branch', true);
    await poll(async () => !(await api(`/trees/${topic.id}`)).some(node => node.id === failed.id || node.id === child.id), 'failed subtree removed from server');
    await card(titles.failed).waitFor({ state: 'hidden' });
    await card(titles.child).waitFor({ state: 'hidden' });
    await card(titles.revision).waitFor();
    await poll(async () => await page.evaluate(({ treeId, nodeId }) => Number(localStorage.getItem(`bl-node:${treeId}`)) === nodeId, { treeId: topic.id, nodeId: topic.root_node_id }), 'deleting current node returns to parent');
    assert.deepEqual(deletes, [`/api/nodes/${failed.id}`]);
    for (const id of [failed.id, child.id]) assert.equal((await fetch(`${base}/api/nodes/${id}/thread`)).status, 404);
    assert.deepEqual(await api(`/nodes/${topic.root_node_id}`), rootBefore, 'Parent content is unchanged');
    const revisionAfter = await api(`/nodes/${revision.id}`);
    assert.deepEqual(revisionAfter.messages, revisionBefore.messages, 'Successful sibling revision messages survive');
    assert.equal(revisionAfter.revision_of, null, 'Deleted revision source reference is detached');
    assert.deepEqual(await api(`/nodes/${grouped.id}`), groupedBefore, 'Another branch is unchanged');
    await page.reload();
    await map.waitFor();
    await card(titles.revision).waitFor();
    assert.equal(await card(titles.failed).count(), 0);
    assert.equal(await card(titles.child).count(), 0);
    checks.push('Confirmed deletion removes failed attempts and descendants, preserves successful sibling revisions, returns to the parent and persists after reload');

    await card(titles.grouped).getByRole('button', { name: `Expand 2 turns in ${titles.grouped}`, exact: true }).click();
    await card(titles.grouped).locator('.lm-round-row').getByRole('button', { name: `Node actions: ${titles.turn}`, exact: true }).click();
    await confirmAction('Delete branch', true);
    await poll(async () => !(await api(`/trees/${topic.id}`)).some(node => node.id === turn.id), 'only selected grouped turn removed');
    await card(titles.grouped).waitFor();
    assert.deepEqual(await api(`/nodes/${grouped.id}`), groupedBefore);
    assert.deepEqual(deletes, [`/api/nodes/${failed.id}`, `/api/nodes/${turn.id}`]);
    checks.push('Deleting an expanded follow-up removes that turn while retaining its group head and unrelated branches');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, checks }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
