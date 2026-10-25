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
from app.models import KnowledgeTree, Memory, Node, PreferenceSupplement
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
    for index in range(13):
        owner = payload[index % 2]
        scope = 'global' if index % 2 else 'topic'
        session.add(PreferenceSupplement(
            content=f'Synthetic {scope} preference {index:02d}: explain with a small worked example.',
            scope=scope, tree_id=owner['tree'] if scope == 'topic' else None,
            source_tree_id=owner['tree'], source_node_id=owner['node'],
            evidence=f'From now on, use worked example style {index:02d} for my learning.',
            status='pending' if index == 12 else 'active',
        ))
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
  async function assertUniformSettingsFrame() {
    const modal = page.getByRole('dialog');
    const original = await modal.boundingBox();
    assert(original);
    for (const name of ['Models & API keys', 'MCP tools', 'Archived', 'Appearance', '语言 / Language', 'Memory']) {
      await modal.locator('.settings-nav').getByRole('button', { name, exact: true }).click();
      const bounds = await modal.boundingBox();
      assert(bounds && ['x', 'y', 'width', 'height'].every(key => Math.abs(bounds[key] - original[key]) < 1), `The ${name} tab keeps the same outer settings frame`);
      const viewport = page.viewportSize();
      assert(bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= viewport.width + 1 && bounds.y + bounds.height <= viewport.height + 1, 'Settings stays inside the viewport');
    }
    assert(await modal.evaluate(element => element.scrollWidth <= element.clientWidth + 2), 'Settings content has no horizontal overflow');
  }
  try {
    await page.goto(base);
    await page.getByRole('heading', { name: trees[0].title, exact: true, level: 1 }).waitFor();
    await openMemory();
    const editor = page.getByTestId('preference-editor');
    await poll(async () => (await editor.inputValue()).includes('Keep technical terms'), 'legacy preferences loaded');
    await assertUniformSettingsFrame();
    checks.push('Every settings tab uses the same outer frame while its content scrolls internally');
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

    const shortHeight = await editor.evaluate(element => element.getBoundingClientRect().height);
    const longPreferences = Array.from({ length: 80 }, (_, index) => `${String(index + 1).padStart(2, '0')}. Use an example before introducing terminology.`).join('\n');
    assert(longPreferences.length < 6000);
    await editor.fill(longPreferences);
    const geometry = await editor.evaluate(element => ({
      height: element.getBoundingClientRect().height,
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      overflowY: getComputedStyle(element).overflowY,
      resize: getComputedStyle(element).resize,
    }));
    assert(Math.abs(geometry.height - 180) < 1 && Math.abs(geometry.height - shortHeight) < 1, 'Long preference content keeps the same fixed editor height');
    assert(geometry.scrollHeight > geometry.clientHeight, 'Long preferences scroll within the textarea');
    assert(['auto', 'scroll'].includes(geometry.overflowY));
    assert.equal(geometry.resize, 'none');
    await editor.evaluate(element => {
      element.scrollTop = 0;
      element.closest('.settings-content').scrollTop = 0;
    });
    await editor.hover();
    const parentScroll = await editor.evaluate(element => element.closest('.settings-content').scrollTop);
    await page.mouse.wheel(0, 180);
    await poll(async () => await editor.evaluate(element => element.scrollTop > 0), 'wheel scrolls preference content');
    assert.equal(await editor.evaluate(element => element.closest('.settings-content').scrollTop), parentScroll, 'Scrolling preferences does not move the settings pane');
    assert.equal(await editor.inputValue(), longPreferences, 'Internal scrolling preserves every character');
    fs.mkdirSync(path.resolve(__dirname, '../artifacts'), { recursive: true });
    await page.screenshot({ path: path.resolve(__dirname, '../artifacts/preference-editor-scroll.png') });
    await editor.fill('x'.repeat(6001));
    assert.equal((await editor.inputValue()).length, 6001, 'Over-limit draft text is retained for editing, never silently truncated');
    assert(await page.getByRole('button', { name: 'Save preferences', exact: true }).isDisabled(), 'An over-limit preference cannot be saved');
    await editor.fill(longPreferences);
    await page.getByRole('button', { name: 'Save preferences', exact: true }).click();
    await poll(async () => (await api('/memories/preferences')).content === longPreferences, 'the full multiline profile is saved');
    await page.reload();
    await openMemory();
    assert.equal(await editor.inputValue(), longPreferences);
    assert(Math.abs((await editor.boundingBox()).height - shortHeight) < 1);
    checks.push('Long preferences use a fixed 180px editor with internal scrolling; all content persists and over-limit drafts remain intact');

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

    // A fact can be kept out of answers without deleting it, and used again.
    const factsOf = async () => (await api(`/memories/facts?tree_id=${trees[1].id}`)).items;
    await rows.first().getByRole('button', { name: 'Stop using memory', exact: true }).click();
    await poll(async () => (await factsOf()).filter(item => item.muted).length === 1, 'fact muted');
    await rows.first().locator('.memory-pending', { hasText: 'Not used' }).waitFor();
    assert.equal((await factsOf()).length, 4, 'A muted fact stays listed');
    await rows.first().getByRole('button', { name: 'Use memory again', exact: true }).click();
    await poll(async () => !(await factsOf()).some(item => item.muted), 'fact used again');
    checks.push('A fact can be stopped from reaching answers and used again from memory settings, and stays listed');

    const supplements = page.getByTestId('preference-supplements');
    const supplementRows = page.getByTestId('preference-supplement');
    const additionsButton = supplements.getByRole('button', { name: /^AI additions ·/ });
    await poll(async () => (await additionsButton.innerText()).includes('13'), 'AI additions count loads while collapsed');
    assert.equal(await additionsButton.getAttribute('aria-expanded'), 'false');
    assert.equal(await supplementRows.count(), 0, 'AI additions start collapsed');
    const storedProfile = await api('/memories/preferences');
    const coreDraft = 'Keep this unsaved core preference while I manage AI additions.';
    await editor.fill(coreDraft);
    await additionsButton.click();
    await poll(async () => await supplementRows.count() === 10, 'AI additions use ten-item pages');
    const additionList = supplements.getByRole('region', { name: 'AI additions list', exact: true });
    const additionGeometry = await additionList.evaluate(element => ({ height: element.getBoundingClientRect().height, content: element.scrollHeight, viewport: element.clientHeight, overflow: getComputedStyle(element).overflowY }));
    assert(additionGeometry.height <= 321 && additionGeometry.content > additionGeometry.viewport);
    assert(['auto', 'scroll'].includes(additionGeometry.overflow));
    assert(await supplements.getByText('Global', { exact: true }).count() > 0);
    assert(await supplements.getByText('Topic only', { exact: true }).count() > 0);
    const pendingRow = supplements.locator('[data-testid="preference-supplement"][data-status="pending"]');
    assert.equal(await pendingRow.count(), 1);
    assert((await pendingRow.innerText()).includes('Not used yet'));
    await pendingRow.getByRole('button', { name: 'Edit preferences', exact: true }).click();
    assert(await editor.evaluate(element => document.activeElement === element), 'A pending suggestion can focus the core profile');
    assert.equal(await editor.inputValue(), coreDraft, 'Focusing core preferences never pastes over the unsaved profile');
    await pendingRow.getByRole('button', { name: 'Edit AI addition', exact: true }).click();
    const additionEditor = supplements.getByRole('textbox', { name: 'Edit AI addition', exact: true });
    const pendingEdit = 'Edited pending preference: keep the contradiction for me to resolve.';
    await additionEditor.fill(pendingEdit);
    await page.getByRole('dialog').locator('.settings-nav').getByRole('button', { name: 'Appearance', exact: true }).click();
    await page.getByRole('dialog').locator('.settings-nav').getByRole('button', { name: 'Memory', exact: true }).click();
    assert.equal(await additionEditor.inputValue(), pendingEdit, 'An addition draft survives a settings-tab switch');
    // Verify the row's own dirty state, independently of the core draft.
    await editor.fill(storedProfile.content);
    page.once('dialog', dialog => dialog.dismiss());
    await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
    assert(await additionEditor.isVisible(), 'Closing settings protects unsaved addition edits');
    const focusStyle = await additionEditor.evaluate(element => { element.focus(); return { outline: getComputedStyle(element).outlineStyle, shadow: getComputedStyle(element).boxShadow }; });
    assert.equal(focusStyle.outline, 'none');
    assert.equal(focusStyle.shadow, 'none');
    await editor.fill(coreDraft);
    await pendingRow.getByRole('button', { name: 'Save', exact: true }).click();
    await poll(async () => (await api('/memories/preferences/supplements')).items.some(item => item.content === pendingEdit && item.status === 'pending' && item.user_edited), 'editing locks the supplement without activating a pending conflict');
    await poll(async () => await additionEditor.count() === 0, 'addition edit finishes');
    assert.equal(await editor.inputValue(), coreDraft);
    assert.equal((await api('/memories/preferences')).content, storedProfile.content, 'Supplement edits never alter the saved core profile');

    const learning = supplements.getByRole('checkbox', { name: 'Remember preferences automatically', exact: true });
    await poll(async () => learning.isEnabled(), 'automatic learning setting loads');
    assert(await learning.isChecked());
    await learning.click();
    await poll(async () => (await api('/memories/preferences/learning')).enabled === false, 'future preference learning is disabled');
    assert.equal((await api('/memories/preferences/supplements')).total, 13, 'Disabling future learning retains existing additions');
    assert.equal(await editor.inputValue(), coreDraft);
    await poll(async () => learning.isEnabled(), 'toggle save completes');
    await poll(async () => !(await learning.isChecked()), 'disabled setting is confirmed in the UI');
    await learning.click();
    await poll(async () => (await api('/memories/preferences/learning')).enabled === true, 'future preference learning can be enabled again');
    await supplements.getByRole('button', { name: 'Refresh AI additions', exact: true }).click();
    await poll(async () => await supplementRows.count() === 10, 'supplements refresh separately');
    assert.equal(await editor.inputValue(), coreDraft, 'Refreshing AI additions does not reset the profile draft');
    await supplements.getByRole('button', { name: 'Next', exact: true }).click();
    await poll(async () => await supplementRows.count() === 3, 'second additions page is bounded');
    await supplements.getByRole('button', { name: 'Previous', exact: true }).click();
    await poll(async () => await supplementRows.count() === 10, 'first additions page restored');
    const activeRow = supplements.locator('[data-testid="preference-supplement"][data-status="active"]').first();
    const deletedContent = await activeRow.locator('.memory-fact-preview').innerText();
    page.once('dialog', dialog => dialog.accept());
    await activeRow.getByRole('button', { name: 'Delete', exact: true }).click();
    await poll(async () => (await api('/memories/preferences/supplements')).total === 12, 'one addition deleted');
    assert(!(await api('/memories/preferences/supplements')).items.some(item => item.content === deletedContent));
    assert.equal(await editor.inputValue(), coreDraft);
    await editor.fill(storedProfile.content);
    checks.push('AI additions are collapsed by default, scoped and paginated with bounded scrolling; editing, pending conflicts, future-learning toggles and deletion preserve both the saved core profile and its local draft');

    // Inspect both compact and expanded Chinese layouts without changing any
    // user-authored preference text or losing the component's state.
    const settings = page.getByRole('dialog');
    await settings.locator('.settings-nav').getByRole('button', { name: '语言 / Language', exact: true }).click();
    await settings.getByRole('button', { name: '中文', exact: true }).click();
    await settings.locator('.settings-nav').getByRole('button', { name: '记忆', exact: true }).click();
    await poll(async () => await supplementRows.count() === 10, 'Chinese additions layout is loaded');
    await settings.locator('.settings-content').evaluate(element => { element.scrollTop = 0; });
    fs.mkdirSync(path.resolve(__dirname, '../artifacts'), { recursive: true });
    await page.screenshot({ path: path.resolve(__dirname, '../artifacts/preferences-additions-expanded-zh.png') });
    await supplements.getByRole('button', { name: /^AI 补充 ·/ }).click();
    await settings.locator('.settings-content').evaluate(element => { element.scrollTop = 0; });
    await page.screenshot({ path: path.resolve(__dirname, '../artifacts/preferences-additions-collapsed-zh.png') });
    await supplements.getByRole('button', { name: /^AI 补充 ·/ }).click();
    await page.setViewportSize({ width: 390, height: 844 });
    await settings.locator('.settings-content').evaluate(element => { element.scrollTop = 0; });
    await page.screenshot({ path: path.resolve(__dirname, '../artifacts/preferences-additions-mobile-zh.png') });
    assert(await settings.evaluate(element => element.scrollWidth <= element.clientWidth + 2), 'AI additions fit a narrow settings pane');
    await settings.locator('.settings-nav').getByRole('button', { name: '语言 / Language', exact: true }).click();
    await settings.getByRole('button', { name: 'English', exact: true }).click();
    await settings.locator('.settings-nav').getByRole('button', { name: 'Memory', exact: true }).click();
    await assertUniformSettingsFrame();
    await page.setViewportSize({ width: 1440, height: 1000 });
    checks.push('The same settings frame fits every tab on desktop and mobile; bilingual AI additions retain their state');

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

    await openMemory();
    await page.getByTestId('preference-supplements').getByRole('button', { name: /^AI additions ·/ }).click();
    const sourceItem = (await api('/memories/preferences/supplements')).items.find(item => item.source_tree_id === trees[0].id);
    assert(sourceItem);
    const sourceRow = page.getByTestId('preference-supplement').filter({ hasText: sourceItem.content });
    await sourceRow.getByRole('button', { name: 'View source', exact: true }).click();
    await page.getByRole('heading', { name: trees[0].title, exact: true, level: 1 }).waitFor();
    assert.equal(await page.getByRole('dialog').count(), 0);
    assert.equal((await api('/memories/preferences')).content, storedProfile.content);
    checks.push('An AI addition opens its original source topic and leaves core preferences unchanged');

    // Stored provenance can outlive its source: a deleted topic or node must not empty the workspace.
    const goneTopic = await api('/trees', { title: 'Memory UI check · Deleted source' });
    const goneNode = (await api(`/trees/${goneTopic.id}`))[0].id;
    assert((await fetch(`${base}/api/trees/${goneTopic.id}`, { method: 'DELETE' })).ok);
    const ownRoot = roots[0][0].id;
    const peerRoot = roots[1][0].id;
    const referenced = [
      { memory_id: 901, source_node_id: goneNode, tree_id: goneTopic.id, tree_title: goneTopic.title, scope: 'other' },
      { memory_id: 902, source_node_id: 999902, tree_id: trees[0].id, tree_title: trees[0].title, scope: 'tree' },
      { memory_id: 903, source_node_id: 999903, tree_id: trees[1].id, tree_title: trees[1].title, scope: 'other' },
      { memory_id: 904, source_node_id: peerRoot, tree_id: trees[1].id, tree_title: trees[1].title, scope: 'other' },
    ];
    await page.route(`**/api/nodes/${ownRoot}/thread`, async route => {
      const response = await route.fetch();
      const body = await response.json();
      body.nodes = body.nodes.map(row => row.node_id === ownRoot
        ? { ...row, question: 'Which memories shaped this answer?', answer: 'A synthetic answer with stored provenance.', status: 'complete', sources: referenced }
        : row);
      await route.fulfill({ response, json: body });
    });
    await page.reload();
    await page.getByRole('heading', { name: trees[0].title, exact: true, level: 1 }).waitFor();
    const chips = page.locator('.chat-sources .chat-source-link');
    await poll(async () => await chips.count() === 4, 'stored sources render as chips');
    for (const index of [0, 1]) {
      assert(await chips.nth(index).isDisabled(), 'A chip whose topic or node no longer exists cannot be opened');
      assert.equal(await chips.nth(index).getAttribute('title'), 'Source deleted');
    }
    assert(await chips.nth(2).isEnabled() && await chips.nth(3).isEnabled());
    await chips.nth(2).click();
    await page.locator('.notice').filter({ hasText: 'This source was deleted.' }).waitFor();
    assert.equal(await page.getByRole('heading', { level: 1 }).innerText(), trees[0].title, 'A missing source node leaves the current topic open');
    assert.equal(await page.evaluate(() => localStorage.getItem('bl-tree')), JSON.stringify(trees[0].id));
    assert.equal(await chips.count(), 4, 'The conversation stays in place');
    await chips.nth(3).click();
    await page.getByRole('heading', { name: trees[1].title, exact: true, level: 1 }).waitFor();
    await page.unroute(`**/api/nodes/${ownRoot}/thread`);
    checks.push('Referenced chips whose topic or node was deleted are disabled or report the deletion and keep the current topic; live sources still open');

    // A Referenced chip of a real memory can keep that memory out of later answers.
    // Every page load opens the first topic (see the init script), so its root shows the answer.
    const realFact = (await api(`/memories/facts?tree_id=${trees[0].id}`)).items[0];
    const factOf = async () => (await api(`/memories/facts?tree_id=${trees[0].id}&page_size=100`)).items.find(item => item.id === realFact.id);
    const chipThread = `**/api/nodes/${ownRoot}/thread`;
    await page.route(chipThread, async route => {
      const response = await route.fetch();
      const body = await response.json();
      body.nodes = body.nodes.map(row => row.node_id === ownRoot
        ? { ...row, question: 'Which memory shaped this answer?', answer: 'A synthetic answer.', status: 'complete', sources: [{ memory_id: realFact.id, source_node_id: realFact.source_node_id, tree_id: trees[0].id, tree_title: trees[0].title, scope: 'path', muted: false }] }
        : row);
      await route.fulfill({ response, json: body });
    });
    await page.reload();
    await page.getByRole('heading', { name: trees[0].title, exact: true, level: 1 }).waitFor();
    await page.getByRole('button', { name: `Stop using memory ${realFact.id}`, exact: true }).click();
    await poll(async () => (await factOf()).muted === true, 'chip muted the fact');
    await page.locator('.chat-sources .chat-source-link', { hasText: 'not used' }).waitFor();
    await page.getByRole('button', { name: `Use memory ${realFact.id} again`, exact: true }).click();
    await poll(async () => (await factOf()).muted === false, 'chip used the fact again');
    await page.unroute(chipThread);
    checks.push('A Referenced chip keeps its memory out of later answers and can use it again');

    // Malformed stored data must not blank the app: bad source entries are dropped, and a value
    // that still breaks the conversation shows a fallback while the rest of the workspace works.
    const ownThread = `**/api/nodes/${ownRoot}/thread`;
    let storedRow = { sources: [null, referenced[3], { memory_id: 'x', scope: 'path' }] };
    await page.route(ownThread, async route => {
      const response = await route.fetch();
      const body = await response.json();
      body.nodes = body.nodes.map(row => row.node_id === ownRoot
        ? { ...row, question: 'Which memories shaped this answer?', answer: 'A synthetic answer.', status: 'complete', ...storedRow }
        : row);
      await route.fulfill({ response, json: body });
    });
    await page.reload();
    await page.getByRole('heading', { name: trees[0].title, exact: true, level: 1 }).waitFor();
    await poll(async () => await chips.count() === 1, 'only the well-formed source is shown');
    // React cannot render an object as text, so no field-level guard (such as the number checks
    // of the diagnostics block) keeps this row from throwing. The check asserts the fallback itself,
    // so it fails, rather than passing on a normal render, if the boundary stops catching it.
    storedRow = { question: { text: 'A question stored as an object' } };
    await page.reload();
    const fallback = page.locator('.chat-pane > .chat-error[role="alert"]').filter({ hasText: 'This conversation could not be displayed.' });
    await fallback.waitFor();
    const reload = fallback.getByRole('button', { name: 'Reload', exact: true });
    assert(await reload.isEnabled(), 'The fallback offers Reload');
    assert.equal(await page.locator('.chat-pane .chat-turn, .chat-pane .chat-compose-box').count(), 0, 'The fallback replaces the conversation');
    await Promise.all([page.waitForEvent('load'), reload.click()]);
    await fallback.waitFor();
    const sidebar = page.locator('.sidebar');
    await sidebar.getByRole('button', { name: trees[1].title, exact: true }).click();
    await page.getByRole('heading', { name: trees[1].title, exact: true, level: 1 }).waitFor();
    assert.equal(await fallback.count(), 0, 'Another topic opens normally');
    await page.unroute(ownThread);
    checks.push('A stored answer that cannot be displayed shows a fallback with a working Reload instead of a blank page; other topics still open');

    // On a phone the map starts closed and its toggle sits in the conversation header, which the
    // fallback replaces: the fallback must keep the toggle so another question can still be opened.
    const pair = await api('/trees/import', {
      format: 'branch-learning', version: 1, tree: { title: 'Memory UI check · Fallback' },
      nodes: [
        { id: 1, parent_id: null, title: 'Why do parsers need grammars?', kind: 'root', status: 'complete' },
        { id: 2, parent_id: 1, title: 'What is a grammar rule?', kind: 'branch', status: 'complete', seed_text: 'grammar', source_node_id: 1, source_message_id: 2 },
      ],
      messages: [
        { id: 1, node_id: 1, role: 'user', content: 'Why do parsers need grammars?' },
        { id: 2, node_id: 1, role: 'assistant', content: 'A grammar says which token sequences form a valid program.', answered_by: 'Demo (offline)' },
        { id: 3, node_id: 2, role: 'user', content: 'What is a grammar rule?' },
        { id: 4, node_id: 2, role: 'assistant', content: 'A rule rewrites one symbol as a sequence of symbols.', answered_by: 'Demo (offline)' },
      ],
    });
    const pairNodes = await api(`/trees/${pair.id}`);
    const pairBranch = pairNodes.find(node => node.parent_id !== null);
    const branchThread = `**/api/nodes/${pairBranch.id}/thread`;
    await page.route(branchThread, async route => {
      const response = await route.fetch();
      const body = await response.json();
      body.nodes = body.nodes.map(row => row.node_id === pairBranch.id ? { ...row, question: { text: 'A question stored as an object' } } : row);
      await route.fulfill({ response, json: body });
    });
    await page.evaluate(({ treeId, nodeId }) => localStorage.setItem(`bl-node:${treeId}`, JSON.stringify(nodeId)), { treeId: pair.id, nodeId: pairBranch.id });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    await page.getByRole('heading', { name: trees[0].title, exact: true, level: 1 }).waitFor();
    await page.getByRole('button', { name: 'Open topic list', exact: true }).click();
    await sidebar.getByRole('button', { name: pair.title, exact: true }).click();
    await fallback.waitFor();
    const learningMap = page.locator('.app > .learning-map');
    assert.equal(await learningMap.count(), 0, 'The map starts closed on a phone');
    await page.locator('.chat-pane').getByRole('button', { name: 'Show learning tree', exact: true }).click();
    await learningMap.waitFor();
    await learningMap.getByRole('button', { name: 'Why do parsers need grammars?', exact: true }).click();
    await page.locator('.chat-question').filter({ hasText: 'Why do parsers need grammars?' }).waitFor();
    assert.equal(await fallback.count(), 0, 'The question opened from the map is displayed');
    assert.equal(await learningMap.count(), 0, 'Choosing a question closes the map on a phone');
    await page.unroute(branchThread);
    await page.setViewportSize({ width: 1440, height: 1000 });
    checks.push('On a phone the fallback keeps the learning-tree toggle, so another question of the topic can be opened from the map');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, checks }, null, 2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
