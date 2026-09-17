// Synthetic data only. Run through manage.py e2e with its temporary database.
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { spawnSync } = require('node:child_process');
const { createRequire } = require('node:module');
const projectRequire = createRequire(path.resolve(__dirname, '../web/package.json'));
const { chromium } = projectRequire('playwright');
const base = process.env.LEARNING_TREE_E2E_BASE;
if (process.env.LEARNING_TREE_E2E !== '1' || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base ?? '')) {
  throw new Error('Use: uv run python scripts/manage.py e2e');
}

async function api(route, body, method = 'POST') {
  const response = await fetch(base + '/api' + route, body === undefined ? {} : {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  assert(response.ok, `${route}: ${response.status}`);
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
  const trees = await Promise.all([
    api('/trees', { title: 'Memory UI check · Compilers' }),
    api('/trees', { title: 'Memory UI check · Databases' }),
  ]);
  const roots = await Promise.all(trees.map(tree => api(`/trees/${tree.id}`)));
  const seed = spawnSync(process.env.LEARNING_TREE_E2E_PYTHON, ['-c', `
import json, os, sys
from pathlib import Path
from sqlalchemy.engine import make_url
from sqlmodel import Session
from app.db import engine
from app.models import KnowledgeTree, Memory, Node
database = Path(make_url(os.environ['DATABASE_URL']).database).resolve()
assert os.environ['LEARNING_TREE_E2E'] == '1'
assert database.parent.name.startswith('learning-tree-e2e-')
assert database.name == 'test.db'
payload = json.load(sys.stdin)
with Session(engine) as session:
    for group, item in enumerate(payload):
        tree = session.get(KnowledgeTree, item['tree'])
        node = session.get(Node, item['node'])
        assert tree.title.startswith('Memory UI check') and node.tree_id == tree.id
        node.status = 'complete'
        session.add(node)
        for index in range(24 if group == 0 else 6):
            content = f'Synthetic {"compiler" if group == 0 else "database"} fact {index:02d}.'
            if group == 0 and index == 3:
                content += ' ORCHID-417 marks this one searchable record.'
            session.add(Memory(kind='fact', content=content, tree_id=tree.id, source_node_id=node.id))
    session.add(Memory(kind='preference', content='Explain with a short example first.'))
    session.add(Memory(kind='preference', content='Keep technical terms in English.'))
    session.commit()
`], {
    cwd: path.resolve(__dirname, '..'), encoding: 'utf8',
    input: JSON.stringify(trees.map((tree, index) => ({ tree: tree.id, node: roots[index][0].id }))),
  });
  assert.equal(seed.status, 0, seed.stderr);

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [], checks = [];
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === base) return route.continue();
    errors.push('Unexpected external request');
    return route.abort();
  });
  await context.addInitScript(treeId => {
    localStorage.setItem('learning-tree.locale', 'en');
    localStorage.setItem('bl-tree', JSON.stringify(treeId));
    localStorage.setItem('bl-theme', 'dark');
  }, trees[0].id);
  async function openMemory() {
    await page.getByRole('button', { name: '⚙ Settings', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Memory', exact: true }).click();
    await page.getByTestId('preference-editor').waitFor();
  }
  try {
    await page.goto(base);
    await page.getByRole('heading', { name: trees[0].title, exact: true, level: 1 }).waitFor();
    await openMemory();
    const editor = page.getByTestId('preference-editor');
    await poll(async () => (await editor.inputValue()).includes('Keep technical terms'), 'legacy preferences loaded');
    const changed = 'Use a short example first.\n\nLet me try before giving the solution.';
    await editor.fill(changed);
    await page.getByRole('dialog').getByRole('button', { name: 'Appearance', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Memory', exact: true }).click();
    assert.equal(await editor.inputValue(), changed, 'Draft survives a settings tab switch');
    await page.getByRole('button', { name: 'Save preferences', exact: true }).click();
    await poll(async () => (await api('/memories/preferences')).content === changed, 'preferences saved in real database');
    await page.reload();
    await openMemory();
    await poll(async () => await editor.inputValue() === changed, 'saved profile survives reload');
    checks.push('Legacy preferences become one editable profile; explicit save persists across reloads');

    const rows = page.getByTestId('memory-fact');
    await poll(async () => await rows.count() === 10, 'bounded first facts page');
    const search = page.getByRole('searchbox', { name: 'Search memories', exact: true });
    await search.fill('ORCHID-417');
    await poll(async () => await rows.count() === 1 && (await rows.first().innerText()).includes('ORCHID-417'), 'search all pages');
    await search.fill('');
    const topic = page.getByRole('combobox', { name: 'Filter by topic', exact: true });
    await topic.selectOption(String(trees[1].id));
    await poll(async () => await rows.count() === 6, 'filter by topic');
    checks.push('Thirty synthetic facts are paginated, searchable beyond page one and filterable by topic');

    // A second client must not silently overwrite the editor's unsaved changes.
    const before = await api('/memories/preferences');
    await editor.fill('My unsaved profile draft.');
    await api('/memories/preferences', { content: 'Updated by another window.', revision: before.revision }, 'PUT');
    await page.getByRole('button', { name: 'Save preferences', exact: true }).click();
    await poll(async () => (await page.getByTestId('memory-management').innerText()).includes('another'), 'stale-save explanation');
    assert.equal(await editor.inputValue(), 'My unsaved profile draft.');
    assert.equal((await api('/memories/preferences')).content, 'Updated by another window.');
    checks.push('A concurrent profile update is reported without overwriting either saved content or the local draft');

    page.once('dialog', dialog => dialog.accept());
    await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
    await openMemory();
    await poll(async () => await editor.inputValue() === 'Updated by another window.', 'fresh profile');
    await page.getByRole('combobox', { name: 'Filter by topic', exact: true }).selectOption(String(trees[1].id));
    await poll(async () => await rows.count() === 6, 'topic restored');

    const first = rows.first();
    await first.getByRole('button', { name: 'Edit memory', exact: true }).click();
    const factEditor = page.getByRole('textbox', { name: 'Edit memory', exact: true });
    await factEditor.fill('Corrected synthetic database fact.');
    await first.getByRole('button', { name: 'Save', exact: true }).click();
    await poll(async () => (await api(`/memories/facts?tree_id=${trees[1].id}`)).items.some(item => item.content === 'Corrected synthetic database fact.'), 'fact edit saved');
    await rows.nth(0).getByRole('checkbox').check();
    await rows.nth(1).getByRole('checkbox').check();
    page.once('dialog', dialog => dialog.accept());
    await page.getByRole('button', { name: /Delete selected/ }).click();
    await poll(async () => (await api(`/memories/facts?tree_id=${trees[1].id}`)).total === 4, 'only selected facts deleted');
    checks.push('Inline fact editing and selected-only bulk deletion update the actual backend');

    fs.mkdirSync(path.resolve(__dirname, '../artifacts'), { recursive: true });
    await page.screenshot({ path: path.resolve(__dirname, '../artifacts/memory-management-desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.getByRole('dialog').evaluate(element => element.scrollWidth <= element.clientWidth + 2), 'Modal fits narrow viewport');
    await page.screenshot({ path: path.resolve(__dirname, '../artifacts/memory-management-mobile.png') });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await rows.first().getByRole('button', { name: 'View source', exact: true }).click();
    await page.getByRole('heading', { name: trees[1].title, exact: true, level: 1 }).waitFor();
    assert.equal(await page.getByRole('dialog').count(), 0);
    checks.push('A fact opens its original learning topic; memory settings fit desktop and narrow screens');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, checks }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
