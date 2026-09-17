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
      await dialog.getByRole('button', { name: english ? 'Memory' : '记忆', exact: true }).click();
      const retrieval = dialog.locator('[data-memory-retrieval-state]');
      await poll(async () => ['ready', 'preparing', 'degraded', 'disabled', 'not_installed'].includes(await retrieval.getAttribute('data-memory-retrieval-state')));
      assert((await retrieval.innerText()).includes(english ? 'Memory retrieval' : '记忆检索'));
      assert((await retrieval.innerText()).includes(english ? 'current learning path' : '当前学习路径'));
      await dialog.getByRole('button', { name: english ? 'Close' : '关闭', exact: true }).click();
      await dialog.waitFor({ state: 'hidden' });
    }
    await assertLocale('zh-CN');
    const ordinaryDraft = 'Keep this unsent draft.';
    await input.fill(ordinaryDraft);
    await chooseLocale('en');
    assert.equal(await input.inputValue(), ordinaryDraft);
    checks.push('Settings switches Chinese to English immediately, including LearningTree title, labels and existing draft');
    checks.push('Memory settings loads real retrieval status with a localized source-boundary explanation');

    // Exercise setup UI with local route fixtures; do not download models or
    // change retrieval settings as a side effect of browser smoke tests.
    let retrievalFixture = { state: 'not_installed', embedding_ready: false, reranker_ready: false };
    const retrievalStatusRoute = '**/api/memories/retrieval/status';
    const retrievalPrepareRoute = '**/api/memories/retrieval/prepare';
    let prepareCalls = 0;
    await page.route(retrievalStatusRoute, route => route.fulfill({ json: retrievalFixture }));
    await page.route(retrievalPrepareRoute, async route => {
      assert.equal(route.request().method(), 'POST');
      prepareCalls += 1;
      await route.fulfill({ json: { state: 'preparing', embedding_ready: false, reranker_ready: false } });
      retrievalFixture = { state: 'ready', embedding_ready: true, reranker_ready: true };
    });
    await page.getByRole('button', { name: '⚙ Settings', exact: true }).click();
    const memoryDialog = page.getByRole('dialog');
    await memoryDialog.getByRole('button', { name: 'Memory', exact: true }).click();
    const retrievalPanel = memoryDialog.locator('[data-memory-retrieval-state]');
    await poll(async () => (await retrievalPanel.getAttribute('data-memory-retrieval-state')) === 'not_installed');
    assert((await retrievalPanel.innerText()).includes('Lightweight keyword retrieval is active'));
    assert((await retrievalPanel.innerText()).includes('uv run python scripts/manage.py retrieval'));
    assert.equal(await memoryDialog.getByRole('button', { name: 'Prepare / retry', exact: true }).count(), 0);
    assert.equal(await memoryDialog.getByRole('link', { name: 'Installation and resource requirements', exact: true }).getAttribute('href'), 'https://github.com/Yuyang16Z/learning-tree/blob/main/docs/operations.md');
    assert.equal(prepareCalls, 0, 'A basic install must not request a model download');
    checks.push('Basic installation explains optional retrieval setup without an ineffective model download action');
    retrievalFixture = { state: 'degraded', embedding_ready: false, reranker_ready: false };
    await memoryDialog.getByRole('button', { name: 'Refresh status', exact: true }).click();
    await poll(async () => (await retrievalPanel.getAttribute('data-memory-retrieval-state')) === 'degraded');
    assert((await retrievalPanel.innerText()).includes('keyword retrieval is active'));
    await memoryDialog.getByRole('button', { name: 'Prepare / retry', exact: true }).click();
    await poll(async () => (await retrievalPanel.getAttribute('data-memory-retrieval-state')) === 'ready');
    assert.equal(prepareCalls, 1, 'Preparation is requested once; subsequent polls only read status');
    assert((await retrievalPanel.innerText()).includes('Local semantic retrieval is ready'));
    await memoryDialog.getByRole('button', { name: 'Close', exact: true }).click();
    await page.unroute(retrievalStatusRoute);
    await page.unroute(retrievalPrepareRoute);
    checks.push('Memory settings explains keyword fallback and progresses from local preparation to ready without downloading test models');
    await page.getByRole('button', { name: '⚙ Settings', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Edit', exact: true }).click();
    const modelForm = page.locator('.model-config-form');
    await modelForm.getByText('Advanced', { exact: true }).click();
    const contextWindow = modelForm.getByRole('spinbutton', { name: 'Context window (tokens)', exact: true });
    assert.equal(await contextWindow.inputValue(), '32768');
    await modelForm.getByRole('spinbutton', { name: 'Answer reserve (tokens)', exact: true }).waitFor();
    assert((await modelForm.innerText()).includes('actual output length is controlled by the provider'));
    await modelForm.getByLabel('API format', { exact: true }).selectOption('anthropic');
    await modelForm.getByRole('spinbutton', { name: 'Max output tokens', exact: true }).waitFor();
    assert.equal(await contextWindow.inputValue(), '32768');
    await modelForm.getByLabel('API format', { exact: true }).selectOption('openai');
    const answerReserve = modelForm.getByRole('spinbutton', { name: 'Answer reserve (tokens)', exact: true });
    await contextWindow.fill('8192');
    await answerReserve.fill('7168');
    await modelForm.getByRole('button', { name: 'Save', exact: true }).click();
    assert((await modelForm.getByRole('alert').innerText()).includes('must exceed'));
    assert.equal((await api('/models'))[0].context_window, 32768, 'Invalid budgets must not be persisted');
    await contextWindow.fill('65536');
    await answerReserve.fill('4096');
    await modelForm.getByRole('button', { name: 'Save', exact: true }).click();
    await modelForm.waitFor({ state: 'hidden' });
    assert.equal((await api('/models'))[0].context_window, 65536);
    await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
    checks.push('Model settings expose context capacity for both protocols, reject unusable budgets and persist valid settings');
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
    await page.locator('.chat-question-actions').first().getByRole('button', { name: 'Edit', exact: true }).click();
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
    await page.locator('.chat-answer-actions').first().getByRole('button', { name: 'New branch', exact: true }).click();
    const branch = await poll(async () => (await api(`/trees/${tree.id}`)).find(node => node.kind === 'branch'));
    assert.equal(branch.source_node_id, tree.root_node_id);
    assert(branch.source_message_id);
    assert.equal(branch.title, '');
    assert.equal(branch.title_state, 'empty');
    await page.locator('.learning-map').getByRole('button', { name: 'New branch', exact: true }).waitFor();
    await poll(async () => (await input.inputValue()) === '');
    assert.equal(await input.inputValue(), '', 'A whole-answer branch keeps its composer empty; only an explicitly selected passage is prefilled');
    const branchQuestion = 'How can I compare two evaluation frameworks?';
    await input.fill(branchQuestion);
    await input.press('Enter');
    await poll(async () => (await api(`/nodes/${branch.id}`)).status === 'complete');
    const titledBranch = await api(`/nodes/${branch.id}`);
    assert.equal(titledBranch.title, branchQuestion);
    assert.equal(titledBranch.title_state, 'fallback', 'Offline mock must not claim an AI summary');
    assert.equal(titledBranch.seed_text, branch.seed_text);
    await page.locator('.learning-map').getByRole('button', { name: branchQuestion, exact: true }).waitFor();
    checks.push('New branch uses a placeholder, then its own first question; source text remains separate');
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
