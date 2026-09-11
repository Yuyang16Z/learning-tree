// Run only through manage.py e2e: it owns and deletes a temporary SQLite database.
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { createRequire } = require('node:module');
const projectRequire = createRequire(path.resolve(__dirname, '../web/package.json'));
const { chromium } = projectRequire('playwright');
const base = process.env.LEARNING_TREE_E2E_BASE;
if (process.env.LEARNING_TREE_E2E !== '1' || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base ?? '')) {
  throw new Error('Use: uv run python scripts/manage.py e2e');
}
async function api(route, body) {
  const response = await fetch(base + '/api' + route, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  assert(response.ok, `API ${route}: ${response.status}`);
  return response.json();
}
async function poll(callback) {
  for (let i = 0; i < 120; i++) {
    const result = await callback();
    if (result) return result;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Timed out waiting for local application state');
}
(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  const page = await context.newPage();
  const errors = [], checks = [];
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', route => {
    // No external model/provider/site requests are allowed during this test.
    if (new URL(route.request().url()).origin === base) return route.continue();
    errors.push('Unexpected external browser request');
    return route.abort();
  });
  try {
    const models = await api('/models');
    assert.equal(models.length, 1);
    assert.equal(models[0].llm_model, 'mock');
    const tree = await api('/trees', { title: 'Smoke: learning with branches' });
    await context.addInitScript(({ treeId }) => {
      if (!localStorage.getItem('learning-tree.locale')) {
        localStorage.setItem('learning-tree.locale', 'zh-CN');
      }
      localStorage.setItem('bl-tree', JSON.stringify(treeId));
    }, { treeId: tree.id });
    await page.goto(base);
    await page.getByRole('heading', { name: tree.title, exact: true, level: 1 }).waitFor();
    const input = page.locator('.chat-compose-box textarea');
    async function assertLocale(locale) {
      const english = locale === 'en';
      await poll(async () => page.evaluate(({ locale, title }) => document.documentElement.lang === locale && document.title === title,
        { locale, title: english ? 'LearningTree' : '学习树' }));
      assert.equal((await page.locator('.brand').innerText()).replace('⑂', '').trim(), english ? 'LearningTree' : '学习树');
      assert.equal(await input.getAttribute('aria-label'), english ? 'Enter a question' : '输入问题');
      assert.equal(await page.locator('.chat-send').getAttribute('aria-label'), english ? 'Send question' : '发送问题');
      assert.equal(await page.evaluate(() => localStorage.getItem('learning-tree.locale')), locale);
      assert.equal(await page.locator('h1').innerText(), tree.title, 'User-authored topic is not translated');
    }
    async function chooseLocale(locale) {
      await page.getByRole('button', { name: /^⚙ (设置|Settings)$/, exact: true }).click();
      const dialog = page.getByRole('dialog');
      await dialog.getByRole('button', { name: '语言 / Language', exact: true }).click();
      const english = locale === 'en';
      const choice = dialog.getByRole('button', { name: english ? 'English' : '中文', exact: true });
      await choice.click();
      await dialog.getByRole('heading', { name: english ? 'Settings' : '设置', exact: true }).waitFor();
      assert.equal(await choice.getAttribute('aria-pressed'), 'true');
      await dialog.getByRole('button', { name: english ? 'Models & API keys' : '模型与 API key', exact: true }).waitFor();
      await assertLocale(locale);
      await dialog.getByRole('button', { name: english ? 'Close' : '关闭', exact: true }).click();
      await dialog.waitFor({ state: 'hidden' });
    }
    await assertLocale('zh-CN');
    const ordinaryDraft = 'Keep this unsent draft.';
    await input.fill(ordinaryDraft);
    await chooseLocale('en');
    assert.equal(await input.inputValue(), ordinaryDraft);
    checks.push('Settings switches Chinese to English immediately, including LearningTree title, labels and existing draft');
    await page.reload();
    await input.waitFor();
    await assertLocale('en');
    assert.equal(await input.inputValue(), ordinaryDraft);
    checks.push('English preference and unsent draft persist across reload');
    await input.fill('Explain how a learning tree works.');
    await input.press('Enter');
    await poll(async () => (await input.inputValue()) === '');
    await page.locator(`[data-answer-node="${tree.root_node_id}"]`).waitFor();
    await poll(async () => (await api(`/nodes/${tree.root_node_id}`)).status === 'complete');
    checks.push('Mock answer persists and Enter clears the submitted draft');
    await input.fill(ordinaryDraft);
    const originalAnswer = await page.locator(`[data-answer-node="${tree.root_node_id}"]`).innerText();
    await page.locator('.chat-question').first().hover();
    await page.locator('.chat-question-actions button').first().click();
    await page.locator('.chat-revision').waitFor();
    await input.focus();
    const style = await input.evaluate(el => ({ outline: getComputedStyle(el).outlineStyle, shadow: getComputedStyle(el).boxShadow }));
    assert.equal(style.outline, 'none');
    assert.equal(style.shadow, 'none');
    const revisionDraft = 'This revision stays while the interface language changes.';
    await input.fill(revisionDraft);
    await chooseLocale('zh-CN');
    assert.equal(await input.inputValue(), revisionDraft);
    assert((await page.locator('.chat-revision').innerText()).includes('编辑为新版本'));
    await chooseLocale('en');
    assert.equal(await input.inputValue(), revisionDraft);
    assert((await page.locator('.chat-revision').innerText()).includes('Edit as a new version'));
    assert.equal(await page.locator(`[data-answer-node="${tree.root_node_id}"]`).innerText(), originalAnswer);
    await page.locator('.chat-revision button').click();
    assert.equal(await input.inputValue(), ordinaryDraft);
    checks.push('Editing has no inner focus ring; switching both languages preserves revision and cancel restores the original draft');
    await page.locator('.chat-answer-actions button').first().click();
    const branch = await poll(async () => (await api(`/trees/${tree.id}`)).find(node => node.kind === 'branch'));
    assert.equal(branch.source_node_id, tree.root_node_id);
    assert(branch.source_message_id);
    checks.push('Visible branch action preserves the source answer');
    await chooseLocale('zh-CN');
    await page.reload();
    await input.waitFor();
    await assertLocale('zh-CN');
    checks.push('Switching back to Chinese updates the UI and persists after reload');
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    checks.push('Reload works; narrow layout has no horizontal overflow or page errors');
    const output = path.resolve(__dirname, '../artifacts');
    fs.mkdirSync(output, { recursive: true });
    fs.writeFileSync(path.join(output, 'smoke.json'), JSON.stringify({ passed: true, checks, provider: 'offline mock', database: 'temporary' }, null, 2));
    console.log(JSON.stringify({ passed: true, checks }));
  } finally {
    await context.close();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
