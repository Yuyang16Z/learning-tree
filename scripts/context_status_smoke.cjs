// All model streams are in-browser fixtures; only empty topics use the isolated test API.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createRequire } = require('node:module');
const { chromium } = createRequire(path.resolve(__dirname, '../web/package.json'))('playwright');
const base = process.env.LEARNING_TREE_E2E_BASE;
if (process.env.LEARNING_TREE_E2E !== '1' || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base ?? '') || new URL(base).port === '8099') {
  throw new Error('Use: uv run python scripts/manage.py e2e');
}
const isolated = spawnSync(process.env.LEARNING_TREE_E2E_PYTHON, ['-c', `
import os
from pathlib import Path
from sqlalchemy.engine import make_url
database = Path(make_url(os.environ['DATABASE_URL']).database).resolve()
assert database.parent.name.startswith('learning-tree-e2e-') and database.name == 'test.db'
assert os.environ['DEFAULT_API_KEY'] == 'mock' and os.environ['DEFAULT_LLM_MODEL'] == 'mock'
assert os.environ['DEFAULT_BASE_URL'] == 'https://mock.invalid/v1'
`], { cwd: path.resolve(__dirname, '..'), encoding: 'utf8' });
assert.equal(isolated.status, 0, isolated.stderr);

async function api(route, body) {
  const response = await fetch(`${base}/api${route}`, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  assert(response.ok, `${route}: ${response.status}`);
  return response.json();
}

(async () => {
  const models = await api('/models');
  assert(models.length && models.every(model => model.llm_model === 'mock'));
  const tree = await api('/trees', { title: 'Context status fixture' });
  const other = await api('/trees', { title: 'Unrelated topic fixture' });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  const page = await context.newPage();
  page.setDefaultTimeout(12000);
  const errors = [], checks = [];
  let modelRequests = 0;
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    if (/\/api\/nodes\/\d+\/(ask|explain)$/.test(url.pathname)) {
      modelRequests++;
      return route.abort();
    }
    if (url.origin === base) return route.continue();
    errors.push('Unexpected external request');
    return route.abort();
  });
  await context.addInitScript(({ treeId, modelId }) => {
    localStorage.setItem('learning-tree.locale', 'en');
    localStorage.setItem('bl-tree', JSON.stringify(treeId));
    localStorage.setItem('bl-model', JSON.stringify(modelId));
    localStorage.setItem('bl-theme', 'dark');
    const realFetch = window.fetch.bind(window);
    window.__contextStreams = [];
    window.fetch = async (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href);
      if (/\/api\/nodes\/\d+\/ask$/.test(url.pathname)) {
        const nodeId = Number(url.pathname.split('/').at(-2));
        const request = JSON.parse(init.body);
        let controller;
        const body = new ReadableStream({ start(value) { controller = value; } });
        const send = event => controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`));
        window.__contextStreams.push({ send, close: () => controller.close(), nodeId, request });
        init.signal.addEventListener('abort', () => controller.error(new DOMException('Cancelled', 'AbortError')), { once: true });
        send({ started: true, node_id: nodeId, request_id: request.request_id });
        return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } });
      }
      if (/\/api\/nodes\/\d+\/stop$/.test(url.pathname)) {
        return new Promise(resolve => {
          window.__resolveContextStop = () => resolve(Response.json({ node_id: window.__contextStreams.at(-1).nodeId, status: 'interrupted' }));
        });
      }
      return realFetch(input, init);
    };
  }, { treeId: tree.id, modelId: models[0].id });
  const label = page.locator('.chat-live .chat-answer-label');
  const input = page.locator('.chat-compose-box textarea');
  async function send(event, close = false) {
    await page.evaluate(({ event, close }) => {
      const stream = window.__contextStreams.at(-1);
      stream.send(event);
      if (close) stream.close();
    }, { event, close });
  }
  async function expectLabel(value) {
    await label.filter({ hasText: value }).waitFor();
    assert.equal(await label.innerText(), value);
  }
  async function ask(question, expectedLabel = 'Responding') {
    await input.fill(question);
    await input.press('Enter');
    await expectLabel(expectedLabel);
  }
  async function finish() {
    await send({ done: true }, true);
    await label.waitFor({ state: 'hidden' });
    await page.locator('.chat-send:not(.is-stop)').waitFor();
  }
  try {
    await page.goto(base);
    await page.getByRole('heading', { name: tree.title, level: 1, exact: true }).waitFor();
    await ask('Synthetic context status check');
    await send({ context_status: 'summarizing' });
    await expectLabel('Organizing context…');
    assert.equal(await page.locator('.notice').count(), 0);
    const output = path.resolve(__dirname, '../artifacts');
    fs.mkdirSync(output, { recursive: true });
    await page.screenshot({ path: path.join(output, 'context-status-organizing.png') });
    checks.push('Context organization uses only the existing answer label, with no toast or extra panel');

    await page.getByRole('navigation', { name: 'Learning tree list', exact: true }).getByRole('button', { name: other.title, exact: true }).click();
    await page.getByRole('heading', { name: other.title, exact: true, level: 1 }).waitFor();
    assert.equal(await label.count(), 0, 'Background progress never appears in another topic');
    await page.getByRole('navigation', { name: 'Learning tree list', exact: true }).getByRole('button', { name: tree.title, exact: true }).click();
    await expectLabel('Organizing context…');
    await send({ context_status: 'ready' });
    await expectLabel('Responding');
    await send({ context_status: 'summarizing' });
    await send({ delta: 'Synthetic answer content' });
    await expectLabel('Responding');
    // Totals add up every provider request of the answer; the context is its largest single request.
    await send({ diagnostics: { estimated_input_tokens: 3600, actual_input_tokens: 3300, cached_input_tokens: 2000, output_tokens: 42, requests: 3, largest_request: { estimated_input_tokens: 1234, actual_input_tokens: 1100, cached_input_tokens: 800 }, compacted: false, tool_rounds: 2, calibration_ratio: 1, target_input_tokens: 131072, summary_blocks: 2, timings_ms: { retrieval: 10, tools: 20, summary: 0, model: 4000, total: 4200 } } });
    const diagnosticsSummary = page.locator('.chat-live details.chat-diagnostics summary');
    await diagnosticsSummary.waitFor();
    assert.equal(await diagnosticsSummary.innerText(), 'Context: est. 1,234 · actual 1,100 tokens (cached 800) · 2 tool rounds · 4.2 s');
    assert.match(await page.locator('.chat-live details.chat-diagnostics > div').textContent(), /^Input across 3 requests: est\. 3,600 · actual 3,300 tokens \(cached 2,000\)\n/);
    assert.match(await page.locator('.chat-live details.chat-diagnostics > div').textContent(), /Calibration ratio 1\.00 · Request target 131,072 tokens · 2 history summaries\n/);
    await expectLabel('Responding');
    assert.equal(await page.locator('.notice').count(), 0);
    await finish();
    checks.push('Switching topics hides unrelated progress; ready and actual answer text restore Responding; diagnostics stay a collapsed block under the answer');

    await ask('Fresh request has no prior context status');
    await send({ context_status: 'summarizing' });
    await expectLabel('Organizing context…');
    await page.getByRole('button', { name: 'Stop response', exact: true }).click();
    await expectLabel('Responding');
    await send({ context_status: 'summarizing' });
    await expectLabel('Responding');
    await page.evaluate(() => window.__resolveContextStop());
    await label.waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: 'Send question', exact: true }).waitFor();
    checks.push('Stop immediately clears status and ignores late context events while cancellation is pending');

    await ask('Failure also clears transient status');
    await send({ context_status: 'summarizing' });
    await expectLabel('Organizing context…');
    await send({ error: 'Synthetic failure' }, true);
    await label.waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: 'Send question', exact: true }).waitFor();
    await ask('Next request starts normally after failure');
    await finish();
    checks.push('Completion, failure and the next request cannot retain stale context status');

    await page.getByRole('button', { name: '⚙ Settings', exact: true }).click();
    const settings = page.getByRole('dialog', { name: 'Settings', exact: true });
    await settings.getByRole('button', { name: '语言 / Language', exact: true }).click();
    await settings.getByRole('button', { name: '中文', exact: true }).click();
    await page.getByRole('dialog', { name: '设置', exact: true }).getByRole('button', { name: '关闭', exact: true }).click();
    await ask('中文状态检查', '正在回答');
    await send({ context_status: 'summarizing' });
    await expectLabel('正在整理上下文…');
    await send({ context_status: 'ready' });
    await expectLabel('正在回答');
    await finish();
    checks.push('The compact progress label switches correctly between Chinese and English');
    assert.equal(modelRequests, 0, 'No model request may leave the browser fixture');
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'context-status-smoke.json'), JSON.stringify({ passed: true, checks, provider: 'in-browser stream fixture', database: 'temporary' }, null, 2));
    console.log(JSON.stringify({ passed: true, checks }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
