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
    // The first read of the scope switches fails (a server restart, a phone on a flaky link).
    const scopeRoute = '**/api/memories/retrieval/settings';
    let scopeReads = 0;
    await page.route(scopeRoute, route => {
      if (route.request().method() !== 'GET') return route.continue();
      scopeReads += 1;
      if (scopeReads === 1) return route.fulfill({ status: 502, body: '' });
      if (scopeReads === 2) return route.fulfill({ json: { include_siblings: true, include_other_trees: false, revision: 'changed-elsewhere' } });
      return route.continue();
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
    const pathScope = retrievalPanel.getByRole('checkbox', { name: 'Current learning path (always)', exact: true });
    const siblingScope = retrievalPanel.getByRole('checkbox', { name: 'Other branches of this topic', exact: true });
    const otherScope = retrievalPanel.getByRole('checkbox', { name: 'Other learning topics (except archived ones)', exact: true });
    const showMemoryTabAgain = async () => {
      await memoryDialog.getByRole('button', { name: 'Appearance', exact: true }).click();
      await memoryDialog.getByRole('button', { name: 'Memory', exact: true }).click();
    };
    await retrievalPanel.getByRole('alert').filter({ hasText: 'Request failed (502)' }).waitFor();
    assert(await siblingScope.isDisabled() && await otherScope.isDisabled(), 'Switches that could not be read cannot be changed');
    // Showing the Memory tab again reads them again: the failure is retried, and a change made on
    // another device appears instead of a stale value that would end in a conflict.
    await showMemoryTabAgain();
    await poll(async () => await siblingScope.isEnabled() && await siblingScope.isChecked());
    assert.equal(await retrievalPanel.getByRole('alert').count(), 0, 'A successful read clears the failed one');
    await showMemoryTabAgain();
    await poll(async () => scopeReads === 3 && !(await siblingScope.isChecked()));
    await page.unroute(scopeRoute);
    checks.push('The memory scope switches are read again whenever the Memory tab is shown, so a failed read is retried and changes from another device appear');
    // Wider memory scopes are opt-in toggles saved with a revision; the path stays always on.
    const scopeSaves = [];
    await page.route(scopeRoute, async route => {
      if (route.request().method() !== 'PUT') return route.continue();
      const body = route.request().postDataJSON();
      scopeSaves.push(body);
      await route.fulfill({ json: { ...body, revision: 'saved-revision' } });
    });
    await poll(async () => await siblingScope.isEnabled());
    assert(await pathScope.isChecked() && await pathScope.isDisabled(), 'The current path is always searched');
    assert(!(await siblingScope.isChecked()) && !(await otherScope.isChecked()), 'Wider scopes start off');
    await siblingScope.click();
    await poll(async () => scopeSaves.length === 1);
    assert.deepEqual(scopeSaves[0], { include_siblings: true, include_other_trees: false, revision: 'initial' });
    await poll(async () => siblingScope.isChecked());
    await page.unroute(scopeRoute);
    // A save still running keeps Settings open, and its conflict is reported after a tab switch.
    let releaseScopeSave;
    await page.route(scopeRoute, async route => {
      if (route.request().method() !== 'PUT') return route.continue();
      await new Promise(resolve => { releaseScopeSave = resolve; });
      await route.fulfill({ status: 409, json: { detail: '检索范围设置已在其他窗口更新，请重新载入。' } });
    });
    await otherScope.click();
    await poll(async () => releaseScopeSave !== undefined);
    assert(await memoryDialog.getByRole('button', { name: 'Close', exact: true }).isDisabled(), 'Settings stays open while a scope change is saving');
    await memoryDialog.getByRole('button', { name: 'Appearance', exact: true }).click();
    releaseScopeSave();
    await memoryDialog.getByRole('button', { name: 'Memory', exact: true }).click();
    await retrievalPanel.getByRole('alert').filter({ hasText: 'your change was not saved' }).waitFor();
    assert(!(await otherScope.isChecked()), 'The switch shows the setting saved elsewhere');
    await page.unroute(scopeRoute);
    checks.push('Memory settings offer opt-in sibling and other-topic recall, saving each change with its revision');
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
    // Detection is explicit: the field keeps its default until the button asks the (fixture) provider.
    const discoverRoute = '**/api/models/discover';
    const discoverBodies = [];
    await page.route(discoverRoute, route => {
      discoverBodies.push(route.request().postDataJSON());
      return route.fulfill({ json: { context_window: 1048576, max_output_tokens: null, source: 'catalog', note: '服务商未提供该模型的上下文长度；以下为内置目录的近似值，请核对后保存。' } });
    });
    await modelForm.getByRole('button', { name: 'Detect', exact: true }).click();
    await modelForm.getByRole('status').filter({ hasText: 'approximate value from the built-in catalog' }).waitFor();
    assert.equal(await contextWindow.inputValue(), '1048576');
    assert.equal(discoverBodies.length, 1);
    assert.equal(discoverBodies[0].protocol, 'anthropic');
    assert.equal(discoverBodies[0].api_key, '', 'The browser never holds the stored key; the server reuses it only for the saved protocol and address');
    await page.unroute(discoverRoute);
    // Detection clears an earlier error, holds the field and Save until it answers, and never fills
    // in a window the form would reject (a 4,096-token vLLM max_model_len, say).
    await contextWindow.fill('8192');
    await modelForm.getByRole('spinbutton', { name: 'Max output tokens', exact: true }).fill('7168');
    await modelForm.getByRole('button', { name: 'Save', exact: true }).click();
    await modelForm.getByRole('alert').waitFor();
    let releaseDetection;
    await page.route(discoverRoute, async route => {
      await new Promise(resolve => { releaseDetection = resolve; });
      await route.fulfill({ json: { context_window: 4096, max_output_tokens: null, source: 'provider', note: '已从服务商获取该模型的上限。' } });
    });
    await modelForm.getByRole('button', { name: 'Detect', exact: true }).click();
    assert.equal(await modelForm.getByRole('alert').count(), 0, 'Detecting clears an earlier validation message');
    await poll(async () => releaseDetection !== undefined);
    assert(await contextWindow.isDisabled(), 'The field cannot be edited while a detection would overwrite it');
    assert(await modelForm.getByRole('button', { name: 'Save', exact: true }).isDisabled(), 'Saving waits for the detection');
    releaseDetection();
    await modelForm.getByRole('status').filter({ hasText: 'outside the accepted range' }).waitFor();
    assert.equal(await contextWindow.inputValue(), '8192', 'An out-of-range detection leaves the field unchanged');
    await page.unroute(discoverRoute);
    // Unstubbed: the API format changed in the form, so the server makes no request with the
    // saved key and says so; the stubbed answers above stand in for a provider the form could reach.
    await modelForm.getByRole('button', { name: 'Detect', exact: true }).click();
    await modelForm.getByRole('status').filter({ hasText: 'saved API key is not sent to the new endpoint' }).waitFor();
    assert.equal(await contextWindow.inputValue(), '8192', 'A detection without a provider answer leaves the field unchanged');
    await modelForm.getByLabel('API format', { exact: true }).selectOption('openai');
    assert.equal(await modelForm.getByRole('status').count(), 0, 'A detection note never describes another model');
    const answerReserve = modelForm.getByRole('spinbutton', { name: 'Answer reserve (tokens)', exact: true });
    await contextWindow.fill('8192');
    await answerReserve.fill('7168');
    await modelForm.getByRole('button', { name: 'Save', exact: true }).click();
    assert((await modelForm.getByRole('alert').innerText()).includes('must exceed'));
    assert.equal((await api('/models'))[0].context_window, 32768, 'Invalid budgets must not be persisted');
    await contextWindow.fill('65536');
    await answerReserve.fill('4096');
    // The request size target is optional: blank keeps the automatic one, and it cannot exceed the window.
    const requestTarget = modelForm.getByRole('spinbutton', { name: 'Request size target (tokens)', exact: true });
    assert.equal(await requestTarget.inputValue(), '', 'A model without a target uses the automatic one');
    assert.equal(await requestTarget.getAttribute('placeholder'), 'Automatic: 65536', 'The automatic target is capped at the window being edited');
    await requestTarget.fill('131072');
    await modelForm.getByRole('button', { name: 'Save', exact: true }).click();
    assert.equal(await requestTarget.evaluate(element => element.validity.rangeOverflow), true, 'A target above the window is refused');
    assert(await modelForm.isVisible());
    assert.equal((await api('/models'))[0].target_input_tokens, null, 'A refused target must not be persisted');
    await requestTarget.fill('32768');
    await modelForm.getByRole('button', { name: 'Save', exact: true }).click();
    await modelForm.waitFor({ state: 'hidden' });
    assert.equal((await api('/models'))[0].context_window, 65536);
    assert.equal((await api('/models'))[0].target_input_tokens, 32768);
    await page.getByRole('dialog').getByRole('button', { name: 'Edit', exact: true }).click();
    await modelForm.getByText('Advanced', { exact: true }).click();
    assert.equal(await requestTarget.inputValue(), '32768');
    await requestTarget.fill('');
    await modelForm.getByRole('button', { name: 'Save', exact: true }).click();
    await modelForm.waitFor({ state: 'hidden' });
    assert.equal((await api('/models'))[0].target_input_tokens, null, 'Clearing the field restores the automatic target');
    await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
    checks.push('Model settings expose context capacity for both protocols, detect windows only on request, reject unusable budgets, keep an optional request size target and persist valid settings');
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
    // The map search asks the server's full-text index for answer passages and jumps to them.
    const map = page.getByRole('complementary', { name: 'LearningTree', exact: true });
    await map.getByRole('button', { name: 'Search nodes', exact: true }).click();
    const searchBox = map.getByRole('textbox', { name: 'Search questions or quotes', exact: true });
    await searchBox.fill('兄弟分支没掺进来');
    const historyGroup = map.getByRole('group', { name: 'Matches in history', exact: true });
    await historyGroup.waitFor();
    const historyHit = historyGroup.getByRole('button').first();
    assert((await historyHit.innerText()).includes('Answer'), 'history hits say what kind of record matched');
    assert((await historyHit.innerText()).includes('兄弟分支没掺进来'), 'history hits show a verbatim snippet');
    const rootAnswer = await api(`/nodes/${tree.root_node_id}`);
    const answerMessage = rootAnswer.messages.find(message => message.role === 'assistant');
    await historyHit.click();
    await poll(async () => ((await page.locator(`[data-message-id="${answerMessage.id}"]`).getAttribute('class')) ?? '').includes('chat-source-flash'));
    // The whole-answer flash is also the fallback when no passage matches: the highlight proves the jump.
    await poll(async () => (await page.evaluate(() => [...(CSS.highlights.get('learning-source') ?? [])].map(range => range.toString()))).some(text => text.includes('兄弟分支没掺进来')));
    assert.equal(await map.getByRole('textbox', { name: 'Search questions or quotes', exact: true }).count(), 0, 'Choosing a hit closes the search');
    checks.push('Map search lists full-text matches from the server and jumps to the matching answer passage');
    // Words only the learner's question holds come back as a question hit, labelled as one.
    await map.getByRole('button', { name: 'Search nodes', exact: true }).click();
    await searchBox.fill('learning tree works');
    await historyGroup.waitFor();
    const questionHit = await historyGroup.getByRole('button').first().innerText();
    assert(questionHit.includes('Question · '), `question hits say they matched a question: ${JSON.stringify(questionHit)}`);
    await searchBox.press('Escape');
    await searchBox.waitFor({ state: 'hidden' });
    checks.push('Map search labels a match in a question as a question');
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
