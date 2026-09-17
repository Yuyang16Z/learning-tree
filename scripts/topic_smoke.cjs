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
  const peerTopic = await api('/trees', { title: 'Topic UI check · Next active topic' });
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
  await context.addInitScript(({ treeId, legacyBackup }) => {
    localStorage.setItem('learning-tree.locale', 'en');
    localStorage.setItem('bl-theme', 'dark');
    if (!localStorage.getItem('bl-tree')) localStorage.setItem('bl-tree', JSON.stringify(treeId));
    if (!localStorage.getItem('topic-smoke-legacy-seeded')) {
      localStorage.setItem('bl-recovery', JSON.stringify(legacyBackup));
      localStorage.setItem('topic-smoke-legacy-seeded', '1');
    }
  }, { treeId: topic.id, legacyBackup: originalBackup });
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
    await poll(async () => await page.evaluate(() => localStorage.getItem('bl-recovery') === null), 'obsolete recovery cache is removed at startup');
    assert.equal(await sidebar.getByRole('button', { name: '↶ Restore last deletion', exact: true }).count(), 0);

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
    const streamingSibling = await api(`/nodes/${streamingTopic.root_node_id}/branch`, { seed_text: 'Independent sibling' });
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
              window.__topicSmokeStreamAborted = true;
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
    // A real second tab deletes an unrelated branch and broadcasts the server's
    // complete deletion scope. The active root response must keep its transport.
    const siblingDraftKey = `bl-draft-v2:${streamingTopic.id}:${streamingSibling.id}`;
    await page.evaluate(key => localStorage.setItem(key, JSON.stringify({ key, text: 'Sibling draft', images: [] })), siblingDraftKey);
    const peerPage = await context.newPage();
    try {
      await peerPage.goto(base);
      await peerPage.getByRole('heading', { name: streamingTitle, exact: true, level: 1 }).waitFor();
      await peerPage.evaluate(async ({ treeId, nodeId }) => {
        const response = await fetch(`/api/nodes/${nodeId}`, { method: 'DELETE' });
        if (!response.ok) throw new Error('Synthetic sibling delete failed');
        const { deleted } = await response.json();
        localStorage.setItem('bl-workspace-deleted', JSON.stringify({ treeId, nodeIds: deleted, nonce: crypto.randomUUID() }));
      }, { treeId: streamingTopic.id, nodeId: streamingSibling.id });
      await poll(async () => await page.evaluate(key => localStorage.getItem(key) === null, siblingDraftKey), 'remote sibling draft cleaned');
      assert.equal(await page.evaluate(() => window.__topicSmokeStreamAborted === true), false, 'Remote sibling deletion must not abort the active response');
      assert(await stop.isVisible(), 'The unrelated response keeps its Stop control');
    } finally { await peerPage.close(); }
    checks.push('Deleting a sibling in another tab cleans its draft without aborting an unrelated response');
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

    // Seed related attachments and topic memories in the guarded temporary DB.
    const uploadBody = new FormData();
    uploadBody.append('file', new Blob(['Synthetic parser attachment for permanent deletion.'], { type: 'text/plain' }), 'topic-delete-check.txt');
    const uploaded = await fetch(`${base}/api/nodes/${topic.root_node_id}/documents`, { method: 'POST', body: uploadBody });
    assert(uploaded.ok, `Synthetic document upload: ${uploaded.status}`);
    const document = await uploaded.json();
    const related = spawnSync(process.env.LEARNING_TREE_E2E_PYTHON, ['-c', `
import json, os, sys
from pathlib import Path
from sqlalchemy.engine import make_url
from sqlmodel import Session
from app.db import engine
from app.models import KnowledgeTree, Memory, MemoryEmbedding, Message, Node
from app.documents import DocumentAttachment
payload = json.load(sys.stdin)
database = Path(make_url(os.environ['DATABASE_URL']).database).resolve()
assert os.environ['LEARNING_TREE_E2E'] == '1'
assert database.parent.name.startswith('learning-tree-e2e-') and database.name == 'test.db'
with Session(engine) as session:
    tree = session.get(KnowledgeTree, payload['tree'])
    peer = session.get(KnowledgeTree, payload['peer'])
    assert tree.title.startswith('Topic UI check') and peer.title.startswith('Topic UI check')
    attachment = session.get(DocumentAttachment, payload['document'])
    assert attachment.tree_id == tree.id
    message = session.get(Message, payload['message'])
    assert session.get(Node, message.node_id).tree_id == tree.id
    message.document_ids = [attachment.id]
    session.add(message)
    memory = Memory(kind='fact', content='Synthetic TOPIC-DELETE-417 parser fact.', tree_id=tree.id, source_node_id=payload['branch'])
    survivor = Memory(kind='fact', content='Synthetic TOPIC-SURVIVES-418 independent fact.', tree_id=peer.id, source_node_id=payload['peer_node'])
    session.add(memory)
    session.add(survivor)
    session.flush()
    session.add(MemoryEmbedding(memory_id=memory.id, model_key='synthetic-test', content_hash='synthetic', vector=[1.0, 0.0]))
    session.commit()
    print(json.dumps({'memory': memory.id, 'survivor': survivor.id}))
`], {
      cwd: path.resolve(__dirname, '..'), encoding: 'utf8',
      input: JSON.stringify({ tree: topic.id, peer: peerTopic.id, peer_node: peerTopic.root_node_id, document: document.id, message: originalRoot.messages[0].id, branch: branch.id }),
    });
    assert.equal(related.status, 0, related.stderr);
    const relatedIds = JSON.parse(related.stdout);
    const beforeDeleteNodes = await api(`/trees/${topic.id}`);
    const beforeDeleteRoot = await api(`/nodes/${topic.root_node_id}`);
    const peerRoot = await api(`/nodes/${peerTopic.root_node_id}`);
    const deletedKeys = [
      `bl-draft-v2:${topic.id}:${topic.root_node_id}`, `bl-draft-v2:${topic.id}:${branch.id}`,
      `bl-scroll-v2:${topic.id}:${topic.root_node_id}`, `bl-scroll-v2:${topic.id}:${branch.id}`,
      `bl-note-draft-v2:${topic.id}:none`, `bl-note-draft-v2:${topic.id}:${branch.id}`, `bl-node:${topic.id}`,
    ];
    const survivorKey = `bl-draft-v2:${peerTopic.id}:${peerTopic.root_node_id}`;
    await page.getByRole('textbox', { name: 'Enter a question', exact: true }).fill('An unsent draft owned by the topic being deleted.');
    await page.evaluate(({ keys, survivorKey }) => {
      for (const key of keys) {
        if (!localStorage.getItem(key)) localStorage.setItem(key, key.includes('draft-v2') && !key.includes('note-draft') ? JSON.stringify({ key, text: 'Synthetic branch draft', images: [] }) : '37');
      }
      localStorage.setItem(survivorKey, JSON.stringify({ key: survivorKey, text: 'Keep this other topic draft.', images: [] }));
    }, { keys: deletedKeys, survivorKey });
    const beforeLocal = await page.evaluate(keys => Object.fromEntries(keys.map(key => [key, localStorage.getItem(key)])), deletedKeys);
    const survivorDraft = await page.evaluate(key => localStorage.getItem(key), survivorKey);
    if (await page.getByRole('button', { name: 'Show learning tree', exact: true }).count()) {
      await page.getByRole('button', { name: 'Show learning tree', exact: true }).click();
    }
    await page.getByRole('button', { name: 'Zoom in learning tree', exact: true }).click();
    await page.getByRole('button', { name: 'Zoom in learning tree', exact: true }).click();
    const beforeCamera = await page.locator('.lm-world').getAttribute('style');
    let deleteCalls = 0, exportCalls = 0;
    page.on('request', request => {
      const pathname = new URL(request.url()).pathname;
      if (pathname === `/api/trees/${topic.id}` && request.method() === 'DELETE') deleteCalls += 1;
      if (pathname === `/api/trees/${topic.id}/export`) exportCalls += 1;
    });
    await openMenu(renamedTitle);
    page.once('dialog', async confirmation => {
      assert(confirmation.message().includes('cannot be undone'), 'Confirmation explains permanent deletion');
      assert(confirmation.message().includes(renamedTitle));
      await confirmation.dismiss();
    });
    await menuItem('Delete').click();
    assert.equal(deleteCalls, 0, 'Cancelling confirmation sends no delete request');
    assert(await savedTopic());
    assert.deepEqual(await page.evaluate(keys => Object.fromEntries(keys.map(key => [key, localStorage.getItem(key)])), deletedKeys), beforeLocal);

    const rejectDelete = async route => {
      if (route.request().method() !== 'DELETE') return route.fallback();
      await route.fulfill({ status: 503, json: { detail: 'Synthetic permanent delete unavailable.' } });
    };
    await page.route(routePattern, rejectDelete);
    await openMenu(renamedTitle);
    page.once('dialog', confirmation => confirmation.accept());
    await menuItem('Delete').click();
    await page.getByText('Synthetic permanent delete unavailable.', { exact: true }).first().waitFor();
    assert.equal(deleteCalls, 1);
    await heading(renamedTitle).waitFor();
    assert.equal(await topicButton(renamedTitle).getAttribute('aria-current'), 'page');
    assert.deepEqual(await api(`/trees/${topic.id}`), beforeDeleteNodes);
    assert.deepEqual(await api(`/nodes/${topic.root_node_id}`), beforeDeleteRoot);
    assert.deepEqual(await page.evaluate(keys => Object.fromEntries(keys.map(key => [key, localStorage.getItem(key)])), deletedKeys), beforeLocal, 'A failed delete preserves local drafts and navigation');
    assert.equal(await page.locator('.lm-world').getAttribute('style'), beforeCamera, 'A failed delete preserves the map camera');
    assert.equal(exportCalls, 0, 'Neither a cancelled nor failed delete creates a backup');
    await page.unroute(routePattern, rejectDelete);
    checks.push('Permanent deletion requires confirmation; cancellation and targeted server failure preserve conversations, drafts, navigation and map state');

    await openMenu(renamedTitle);
    page.once('dialog', confirmation => confirmation.accept());
    await menuItem('Delete').click();
    await poll(async () => !(await api('/trees?include_archived=true')).some(tree => tree.id === topic.id), 'permanent delete completed');
    await poll(async () => await page.evaluate(keys => keys.every(key => localStorage.getItem(key) === null), deletedKeys), 'owned draft and navigation keys removed');
    assert.equal(exportCalls, 0, 'Permanent deletion never exports a hidden recovery backup');
    assert.equal(deleteCalls, 2);
    assert.equal(await sidebar.getByRole('button', { name: '↶ Restore last deletion', exact: true }).count(), 0);
    assert.equal(await page.evaluate(() => localStorage.getItem('bl-recovery')), null);
    assert.equal(await page.evaluate(key => localStorage.getItem(key), survivorKey), survivorDraft, 'Another topic draft survives');
    assert.notEqual(await page.evaluate(() => JSON.parse(localStorage.getItem('bl-tree'))), topic.id);
    await page.locator('.lm-card').filter({ has: page.getByText('Original root question', { exact: true }) }).waitFor({ state: 'hidden' });
    assert.notEqual(await page.locator('.lm-world').getAttribute('style'), beforeCamera, 'The next topic has its own map view');
    for (const route of [`/trees/${topic.id}`, `/nodes/${topic.root_node_id}`, `/nodes/${branch.id}/thread`, `/documents/${document.id}`, `/documents/${document.id}/download`]) {
      assert.equal((await fetch(`${base}/api${route}`)).status, 404, `${route} no longer exposes deleted content`);
    }
    const factsAfterDelete = await api('/memories/facts?q=TOPIC-DELETE-417');
    assert.equal(factsAfterDelete.total, 0);
    assert.equal((await api('/memories/facts?q=TOPIC-SURVIVES-418')).total, 1);
    assert.deepEqual(await api(`/nodes/${peerTopic.root_node_id}`), peerRoot, 'Other conversations survive');
    const deletionAudit = spawnSync(process.env.LEARNING_TREE_E2E_PYTHON, ['-c', `
import json, os, sys
from pathlib import Path
from sqlalchemy.engine import make_url
from sqlmodel import Session, select
from app.db import engine
from app.models import KnowledgeTree, Memory, MemoryEmbedding, Message, Node
from app.documents import DocumentAttachment
payload = json.load(sys.stdin)
database = Path(make_url(os.environ['DATABASE_URL']).database).resolve()
assert os.environ['LEARNING_TREE_E2E'] == '1'
assert database.parent.name.startswith('learning-tree-e2e-') and database.name == 'test.db'
with Session(engine) as session:
    assert session.get(KnowledgeTree, payload['tree']) is None
    assert not session.exec(select(Node).where(Node.tree_id == payload['tree'])).all()
    assert not session.exec(select(Message).where(Message.node_id.in_(payload['nodes']))).all()
    assert session.get(DocumentAttachment, payload['document']) is None
    assert session.get(Memory, payload['memory']) is None
    assert not session.exec(select(MemoryEmbedding).where(MemoryEmbedding.memory_id == payload['memory'])).all()
    assert session.get(Memory, payload['survivor']) is not None
`], {
      cwd: path.resolve(__dirname, '..'), encoding: 'utf8',
      input: JSON.stringify({ tree: topic.id, nodes: originalNodes.map(node => node.id), document: document.id, ...relatedIds }),
    });
    assert.equal(deletionAudit.status, 0, deletionAudit.stderr);
    await page.reload();
    await sidebar.waitFor();
    assert(await page.evaluate(keys => keys.every(key => localStorage.getItem(key) === null), deletedKeys), 'Pagehide and reload cannot recreate deleted drafts');
    assert.equal(await page.evaluate(() => localStorage.getItem('bl-recovery')), null);
    checks.push('Confirmed deletion removes the tree, nodes, messages, attachments, memories, vector cache and owned browser state without a hidden backup; other topics survive');

    // A fresh topic covers the last-active archive path without restoring deletion.
    const lastTitle = 'Topic UI check · Last active topic';
    const lastTopic = await api('/trees', { title: lastTitle });
    assert.notEqual(lastTopic.id, topic.id, 'New topics never reuse the deleted topic identity');
    for (const other of await api('/trees')) {
      if (other.id !== lastTopic.id) await api(`/trees/${other.id}`, { archived: true }, 'PATCH');
    }
    await page.evaluate(id => localStorage.setItem('bl-tree', JSON.stringify(id)), lastTopic.id);
    await page.reload();
    await heading(lastTitle).waitFor();
    await openMenu(lastTitle);
    await menuItem('Archive').click();
    await poll(async () => (await api('/trees')).length === 0, 'last active topic archived');
    await heading('Learning space').waitFor();
    await page.getByRole('heading', { name: 'Start with curiosity', exact: true }).waitFor();
    await showArchived();
    await topicButton(lastTitle).click();
    await heading(lastTitle).waitFor();
    checks.push('Archiving the last active topic leaves a clean empty chat and the archived conversation remains accessible');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, checks }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
