// Learning styles in the browser with the offline mock model. Run only through manage.py e2e: it owns the
// temporary database and the temporary custom-style directory this script writes synthetic files into.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { chromium } = createRequire(path.resolve(__dirname, '../web/package.json'))('playwright');
const base = process.env.LEARNING_TREE_E2E_BASE;
const presetsDirectory = process.env.LEARNING_PRESETS_DIR ?? '';
if (process.env.LEARNING_TREE_E2E !== '1' || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base ?? '') || new URL(base).port === '8099'
  || path.basename(presetsDirectory) !== 'learning-presets' || !path.basename(path.dirname(presetsDirectory)).startsWith('learning-tree-e2e-')) {
  throw new Error('Use: uv run python scripts/manage.py e2e');
}

async function api(route, body, method = 'POST') {
  const response = await fetch(`${base}/api${route}`, body === undefined ? {} : {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  assert(response.ok, `${route}: ${response.status}`);
  return response.json();
}
async function poll(callback, label) {
  for (let i = 0; i < 150; i++) {
    const result = await callback();
    if (result) return result;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for ${label}`);
}

(async () => {
  const models = await api('/models');
  assert(models.length && models.every(model => model.llm_model === 'mock'));
  const styled = await api('/trees', { title: 'Learning style fixture' });
  const plain = await api('/trees', { title: 'Default style fixture' });
  fs.mkdirSync(presetsDirectory, { recursive: true });
  const customFile = path.join(presetsDirectory, 'smoke-analogy.md');
  fs.writeFileSync(customFile, '---\ntitle_zh: 合成类比\ntitle_en: Synthetic analogies\ndescription_zh: 合成说明\ndescription_en: A synthetic custom style\norder: 1\n---\nSynthetic instruction for the browser check.\n');
  fs.writeFileSync(path.join(presetsDirectory, 'broken-style.md'), '---\ntitle_zh: 坏\ntitle_en: Broken\ncolour: blue\n---\nSynthetic\n');

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  const page = await context.newPage();
  page.setDefaultTimeout(12000);
  const errors = [], checks = [], asks = [], patches = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    const pathname = new URL(request.url()).pathname;
    if (/^\/api\/nodes\/\d+\/ask$/.test(pathname)) asks.push(request.postDataJSON());
    if (request.method() === 'PATCH' && pathname.startsWith('/api/trees/')) patches.push({ pathname, body: request.postDataJSON() });
  });
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === base) return route.continue();
    errors.push('Unexpected external browser request');
    return route.abort();
  });
  await context.addInitScript(({ treeId, modelId }) => {
    if (!localStorage.getItem('learning-tree.locale')) localStorage.setItem('learning-tree.locale', 'en');
    if (!localStorage.getItem('bl-tree')) localStorage.setItem('bl-tree', JSON.stringify(treeId));
    localStorage.setItem('bl-model', JSON.stringify(modelId));
  }, { treeId: styled.id, modelId: models[0].id });

  const input = page.locator('.chat-compose-box textarea');
  const styleButton = name => page.getByRole('button', { name: `Learning style: ${name}`, exact: true });
  const menu = page.getByRole('menu', { name: 'Learning style · for the next questions in this topic', exact: true });
  const topics = page.getByRole('navigation', { name: 'Learning tree list', exact: true });
  async function openMenu(current) {
    await styleButton(current).waitFor();
    await poll(async () => styleButton(current).isEnabled(), 'an enabled style control');
    await styleButton(current).click();
    await menu.waitFor();
  }
  async function ask(question) {
    const before = asks.length;
    await input.fill(question);
    await input.press('Enter');
    await poll(async () => asks.length > before, 'the ask request');
    await page.locator('.chat-question').filter({ hasText: question }).waitFor();
    await page.getByRole('button', { name: 'Send question', exact: true }).waitFor();
    await poll(async () => (await input.inputValue()) === '', 'the cleared composer');
    return asks[asks.length - 1];
  }
  const turn = question => page.locator('.chat-turn').filter({ has: page.locator('.chat-question').filter({ hasText: question }) });
  const tagOf = question => turn(question).locator('.chat-question .chat-style-tag');
  try {
    await page.goto(base);
    await page.getByRole('heading', { name: styled.title, exact: true, level: 1 }).waitFor();
    await openMenu('Default');
    const names = await menu.getByRole('menuitemradio').evaluateAll(items => items.map(item => [item.getAttribute('aria-label'), item.getAttribute('aria-checked')]));
    assert.deepEqual(names, [
      ['Default (direct explanation)', 'true'], ['Socratic questions', 'false'], ['Explain it back', 'false'],
      ['Worked examples', 'false'], ['Compare and contrast', 'false'], ['Mistake review', 'false'],
      ['Self-test', 'false'], ['Synthetic analogies', 'false'],
    ]);
    const dropdown = page.locator('.chat-style-dropdown');
    assert((await dropdown.innerText()).includes('broken-style.md'), 'an unusable custom file is named');
    assert((await dropdown.innerText()).includes('The settings block may only use title_zh, title_en, description_zh, description_en and order.'));
    assert(!(await dropdown.innerText()).includes('blue'), 'file contents are never shown');
    assert.equal(await dropdown.getByRole('link', { name: 'docs/learning-presets.md', exact: true }).getAttribute('href'), 'https://github.com/Yuyang16Z/learning-tree/blob/main/docs/learning-presets.md');
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), 'Socratic questions', 'arrow keys move focus without choosing');
    assert.deepEqual(patches, []);
    await page.keyboard.press('Escape');
    await menu.waitFor({ state: 'hidden' });
    checks.push('The style menu lists Default first, the built-ins in order and custom styles after them, names unusable files without their contents and links the docs');

    await openMenu('Default');
    const saved = page.waitForResponse(response => response.request().method() === 'PATCH' && new URL(response.url()).pathname === `/api/trees/${styled.id}`);
    await menu.getByRole('menuitemradio', { name: 'Socratic questions', exact: true }).click();
    assert.equal((await saved).status(), 200);
    assert.deepEqual(patches, [{ pathname: `/api/trees/${styled.id}`, body: { learning_preset: 'socratic' } }]);
    await styleButton('Socratic questions').waitFor();
    assert.equal((await api('/trees')).find(tree => tree.id === styled.id).learning_preset, 'socratic');
    const socraticAsk = await ask('Synthetic question asked Socratically');
    assert.equal(socraticAsk.learning_preset, 'socratic', 'the request carries the chosen style');
    await poll(async () => (await api(`/nodes/${styled.root_node_id}`)).status === 'complete', 'the mock answer');
    assert.equal((await api(`/nodes/${styled.root_node_id}`)).learning_preset, 'socratic');
    await tagOf('Synthetic question asked Socratically').waitFor();
    // The visible title follows a screen-reader-only "Learning style:" prefix.
    assert.equal(await tagOf('Synthetic question asked Socratically').textContent(), 'Learning style: Socratic questions');
    assert(!(await turn('Synthetic question asked Socratically').locator('.chat-answer-label').innerText()).includes('Socratic'), 'the answer label is left alone');
    checks.push('Choosing a style saves the topic default, the next question carries it and its bubble shows the style tag');

    await page.reload();
    await styleButton('Socratic questions').waitFor();
    await tagOf('Synthetic question asked Socratically').waitFor();
    await topics.getByRole('button', { name: plain.title, exact: true }).click();
    await page.getByRole('heading', { name: plain.title, exact: true, level: 1 }).waitFor();
    await styleButton('Default').waitFor();
    const plainAsk = await ask('Synthetic question in the default way');
    assert.equal(plainAsk.learning_preset, null, 'the request says the default way explicitly');
    await poll(async () => (await api(`/nodes/${plain.root_node_id}`)).status === 'complete', 'the second mock answer');
    assert.equal(await tagOf('Synthetic question in the default way').count(), 0);
    await topics.getByRole('button', { name: styled.title, exact: true }).click();
    await page.getByRole('heading', { name: styled.title, exact: true, level: 1 }).waitFor();
    await styleButton('Socratic questions').waitFor();
    checks.push('Reload keeps the topic default, and another topic keeps its own default and sends null');

    await page.evaluate(() => localStorage.setItem('learning-tree.locale', 'zh-CN'));
    await page.reload();
    await page.getByRole('button', { name: '学习方式：苏格拉底提问', exact: true }).waitFor();
    await poll(async () => (await tagOf('Synthetic question asked Socratically').textContent()) === '学习方式：苏格拉底提问', 'the Chinese tag');
    await page.evaluate(() => localStorage.setItem('learning-tree.locale', 'en'));
    await page.reload();
    await styleButton('Socratic questions').waitFor();
    checks.push('The control and tags follow the interface language');

    await page.route(`**/api/trees/${styled.id}`, route => route.request().method() === 'PATCH' ? route.fulfill({ status: 500, body: '' }) : route.continue());
    await openMenu('Socratic questions');
    await menu.getByRole('menuitemradio', { name: 'Explain it back', exact: true }).click();
    await page.locator('.chat-error').filter({ hasText: 'The learning style was not saved; the previous choice is back.' }).waitFor();
    await styleButton('Socratic questions').waitFor();
    assert.equal((await api('/trees')).find(tree => tree.id === styled.id).learning_preset, 'socratic');
    await page.unroute(`**/api/trees/${styled.id}`);
    checks.push('A failed save restores the previous style and says so');

    await openMenu('Socratic questions');
    const savedCustom = page.waitForResponse(response => response.request().method() === 'PATCH' && new URL(response.url()).pathname === `/api/trees/${styled.id}`);
    await menu.getByRole('menuitemradio', { name: 'Synthetic analogies', exact: true }).click();
    assert.equal((await savedCustom).status(), 200);
    await styleButton('Synthetic analogies').waitFor();
    const customAsk = await ask('Synthetic question with a custom style');
    assert.equal(customAsk.learning_preset, 'custom:smoke-analogy');
    await tagOf('Synthetic question with a custom style').filter({ hasText: 'Synthetic analogies' }).waitFor();
    fs.unlinkSync(customFile);
    await page.reload();
    await styleButton('Unavailable').waitFor();
    await tagOf('Synthetic question with a custom style').filter({ hasText: 'Unavailable' }).waitFor();
    await openMenu('Unavailable');
    const vanished = menu.getByRole('menuitemradio', { name: 'Unavailable', exact: true });
    assert.equal(await vanished.getAttribute('aria-checked'), 'true');
    assert.equal(await vanished.getAttribute('aria-disabled'), 'true');
    assert((await page.locator('.chat-style-dropdown').innerText()).includes('“custom:smoke-analogy” was removed or cannot be read'));
    await page.keyboard.press('Escape');
    const afterDeletion = await ask('Synthetic question after the custom style was deleted');
    assert.equal(afterDeletion.learning_preset, null, 'a deleted style is sent as the default way, not rejected');
    await poll(async () => (await api(`/trees/${styled.id}`)).every(node => node.status !== 'pending'), 'the last mock answer');
    assert.equal(await tagOf('Synthetic question after the custom style was deleted').count(), 0);
    checks.push('A deleted custom style is shown as unavailable, its old questions keep a tag, and new questions use the default way');

    await page.setViewportSize({ width: 390, height: 844 });
    await openMenu('Unavailable');
    const bounds = await page.locator('.chat-style-dropdown').boundingBox();
    assert(bounds && bounds.x >= 0 && bounds.x + bounds.width <= 391, 'The style menu fits a narrow screen');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    checks.push('The control and its menu fit a 390 px screen without horizontal scrolling');
    assert.deepEqual(errors, []);
    const output = path.resolve(__dirname, '../artifacts');
    fs.mkdirSync(output, { recursive: true });
    fs.writeFileSync(path.join(output, 'learning-presets-smoke.json'), JSON.stringify({ passed: true, checks, provider: 'offline mock', database: 'temporary' }, null, 2));
    console.log(JSON.stringify({ passed: true, checks }));
  } finally {
    await context.close();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
