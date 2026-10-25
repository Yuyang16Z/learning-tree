// MCP configuration is a browser route fixture. Questions use the real, isolated offline demo.
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
async function poll(callback, label) {
  for (let index = 0; index < 120; index++) {
    if (await callback()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${label}`);
}

(async () => {
  const models = await api('/models');
  assert(models.length > 0 && models.every(model => model.llm_model === 'mock'), 'Only offline demo models are permitted');
  const tree = await api('/trees', { title: 'Combined web tool UI check' });
  const combinedId = 99101;
  let combined = {
    id: combinedId, label: '联网搜索', command: '/synthetic/python',
    args: ['/synthetic/learning-tree/integrations/mcp/launch.py', 'web-research'], enabled: false,
  };
  const unrelated = { id: 99102, label: 'Fixture notes', command: '/synthetic/never-launched', args: [], enabled: true };
  const remoteId = 99103;
  let remote = null;
  const remoteBodies = [];
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  const page = await context.newPage();
  page.setDefaultTimeout(12000);
  const errors = [], checks = [], asks = [], fixtureUpdates = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (/\/api\/nodes\/\d+\/ask$/.test(new URL(request.url()).pathname)) asks.push(request.postDataJSON());
  });
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === base) return route.continue();
    errors.push('Unexpected external browser request');
    return route.abort();
  });
  await page.route('**/api/mcp', route => {
    if (route.request().method() === 'POST') {
      // The route stands in for the backend, which keeps header values and lists names only.
      const body = route.request().postDataJSON();
      remoteBodies.push(body);
      const { headers, ...stored } = body;
      remote = { ...stored, id: remoteId, header_names: Object.keys(headers ?? {}) };
      return route.fulfill({ json: remote });
    }
    assert.equal(route.request().method(), 'GET');
    return route.fulfill({ json: [combined, unrelated, ...(remote ? [remote] : [])] });
  });
  await page.route(`**/api/mcp/${combinedId}`, route => {
    assert.equal(route.request().method(), 'PUT');
    const body = route.request().postDataJSON();
    fixtureUpdates.push(body);
    combined = { ...body, id: combinedId };
    return route.fulfill({ json: combined });
  });
  // A UI regression must not launch a process by testing a synthetic MCP server.
  await page.route('**/api/mcp/*/test', route => {
    errors.push('Unexpected MCP connection test');
    return route.abort();
  });
  await context.addInitScript(({ treeId, modelId }) => {
    localStorage.setItem('learning-tree.locale', 'en');
    localStorage.setItem('bl-theme', 'dark');
    localStorage.setItem('bl-tree', JSON.stringify(treeId));
    localStorage.setItem('bl-model', JSON.stringify(modelId));
  }, { treeId: tree.id, modelId: models[0].id });

  const toolButton = page.getByRole('button', { name: 'Choose tools', exact: true });
  const menu = page.locator('.chat-tool-dropdown');
  const input = page.locator('.chat-compose-box textarea');
  async function openTools() {
    if (await toolButton.getAttribute('aria-expanded') !== 'true') await toolButton.click();
    await menu.waitFor();
  }
  async function closeTools() {
    if (await toolButton.getAttribute('aria-expanded') === 'true') await toolButton.click();
  }
  async function setCombinedEnabled(enabled) {
    await closeTools();
    await page.getByRole('button', { name: '⚙ Settings', exact: true }).click();
    const settings = page.getByRole('dialog', { name: 'Settings', exact: true });
    await settings.getByRole('button', { name: 'MCP tools', exact: true }).click();
    await settings.getByRole('button', { name: `Web search, ${combined.enabled ? 'enabled' : 'disabled'}, edit configuration`, exact: true }).click();
    const configuration = page.getByRole('dialog', { name: 'MCP configuration', exact: true });
    await configuration.getByRole('checkbox', { name: 'Enabled', exact: true }).setChecked(enabled);
    await configuration.getByRole('button', { name: 'Save', exact: true }).click();
    await configuration.waitFor({ state: 'hidden' });
    assert.equal(combined.enabled, enabled);
    await settings.getByRole('button', { name: 'Close', exact: true }).click();
    await settings.waitFor({ state: 'hidden' });
  }
  async function ask(question, expectedTools) {
    await closeTools();
    await input.fill(question);
    const responsePromise = page.waitForResponse(response => /\/api\/nodes\/\d+\/ask$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST');
    await input.press('Enter');
    const response = await responsePromise;
    assert.equal(response.status(), 200);
    assert.equal(response.request().postDataJSON().config_id, models[0].id);
    assert.deepEqual(response.request().postDataJSON().tools ?? [], expectedTools, question);
    assert.equal(await response.finished(), null);
    await page.getByRole('button', { name: 'Send question', exact: true }).waitFor();
    await page.locator('.chat-question').filter({ hasText: question }).waitFor();
    assert.equal(await input.inputValue(), '');
    assert.equal(await page.locator('.chat-error').count(), 0);
    await poll(async () => {
      const nodes = await api(`/trees/${tree.id}`);
      return nodes.every(node => node.status === 'complete');
    }, 'the real offline answer is persisted');
  }

  try {
    await page.goto(base);
    await page.getByRole('heading', { name: tree.title, exact: true, level: 1 }).waitFor();
    await openTools();
    assert.equal(await menu.getByRole('checkbox').count(), 3, 'Disabled combined MCP leaves two built-ins and the unrelated MCP');
    await menu.getByRole('checkbox', { name: 'Web search', exact: true }).check();
    await menu.getByRole('checkbox', { name: 'Read webpage', exact: true }).check();
    assert.equal(await menu.getByRole('checkbox', { name: 'Fixture notes', exact: true }).isChecked(), false);
    await ask('Offline baseline with the two existing web tools.', ['web_search', 'fetch']);
    checks.push('Without an enabled combined preset, both existing built-in controls remain available and submit their original tool identifiers');

    // Keep the old built-ins checked across a settings update, as an existing user can.
    await setCombinedEnabled(true);
    await openTools();
    assert.equal(await menu.getByRole('checkbox').count(), 2);
    assert.equal(await menu.getByRole('checkbox', { name: 'Web search', exact: true }).count(), 1, 'Only the combined Web search entry remains');
    assert.equal(await menu.getByRole('checkbox', { name: 'Read webpage', exact: true }).count(), 0);
    assert.equal(await menu.getByRole('checkbox', { name: 'Web search', exact: true }).isChecked(), false);
    assert.equal(await menu.getByRole('checkbox', { name: 'Fixture notes', exact: true }).count(), 1, 'Other enabled MCP servers remain independently selectable');
    await ask('Offline check that hidden selections do not leak.', []);
    checks.push('Enabling the combined preset replaces the duplicate controls and old hidden built-in selections cannot leak into a later request');

    await openTools();
    await menu.getByRole('checkbox', { name: 'Web search', exact: true }).check();
    assert.equal(await toolButton.locator('.chat-tool-count').innerText(), '1');
    await ask('Offline check with the combined web group selected.', [`mcp_server_${combinedId}`]);
    checks.push('Selecting the single Web search entry sends only its MCP server identifier and counts as one group');

    await openTools();
    await menu.getByRole('checkbox', { name: 'Web search', exact: true }).uncheck();
    assert.equal(await toolButton.locator('.chat-tool-count').count(), 0);
    await ask('Offline check with web access unchecked.', []);
    checks.push('Unchecking the combined entry removes all web tools from the next request');

    // Disabling a selected MCP in Settings must not silently reactivate old built-ins.
    await openTools();
    await menu.getByRole('checkbox', { name: 'Web search', exact: true }).check();
    await setCombinedEnabled(false);
    await openTools();
    assert.equal(await menu.getByRole('checkbox').count(), 3);
    assert.equal(await menu.getByRole('checkbox', { name: 'Web search', exact: true }).isChecked(), false);
    assert.equal(await menu.getByRole('checkbox', { name: 'Read webpage', exact: true }).isChecked(), false);
    await ask('Offline check after disabling a selected web preset.', []);
    checks.push('Disabling a selected combined MCP restores unchecked built-in controls without silently re-enabling web access');

    await closeTools();
    await page.getByRole('button', { name: '⚙ Settings', exact: true }).click();
    const settingsDialog = page.getByRole('dialog', { name: 'Settings', exact: true });
    await settingsDialog.getByRole('button', { name: 'MCP tools', exact: true }).click();
    await settingsDialog.getByRole('button', { name: '＋ Add MCP', exact: true }).click();
    const add = page.getByRole('dialog', { name: 'Add MCP', exact: true });
    await add.getByRole('textbox', { name: 'Display name', exact: true }).fill('Fixture remote docs');
    await add.getByRole('radio', { name: 'Remote HTTP', exact: true }).check();
    assert.equal(await add.getByRole('textbox', { name: 'Launch command', exact: true }).count(), 0);
    await add.getByRole('button', { name: 'Add', exact: true }).click();
    await add.getByRole('alert').filter({ hasText: 'Enter a display name and server URL.' }).waitFor();
    await add.getByRole('textbox', { name: 'Server URL', exact: true }).fill('https://docs.example/mcp');
    await add.getByRole('button', { name: '＋ Add header', exact: true }).click();
    await add.getByRole('textbox', { name: 'Header name', exact: true }).fill('Authorization');
    await add.getByLabel('Header value', { exact: true }).fill('Bearer fixture-token');
    await add.getByRole('checkbox', { name: 'Enabled', exact: true }).uncheck();
    // New servers ask before tools they do not mark read-only unless another policy is chosen.
    const askPolicy = add.getByLabel('Ask before running tools', { exact: true });
    assert.equal(await askPolicy.inputValue(), 'writes');
    await askPolicy.selectOption('always');
    await add.getByRole('button', { name: 'Add', exact: true }).click();
    await add.waitFor({ state: 'hidden' });
    assert.deepEqual(remoteBodies, [{
      label: 'Fixture remote docs', transport: 'http', command: '', args: [], url: 'https://docs.example/mcp',
      headers: { Authorization: 'Bearer fixture-token' }, allow_private: false, enabled: false, confirm_tools: 'always',
    }]);
    const remoteRow = settingsDialog.getByRole('button', { name: 'Fixture remote docs, disabled, edit configuration', exact: true });
    assert.equal(await remoteRow.locator('.k').innerText(), 'docs.example');
    await remoteRow.click();
    const edit = page.getByRole('dialog', { name: 'MCP configuration', exact: true });
    assert.equal(await edit.getByRole('radio', { name: 'Remote HTTP', exact: true }).isChecked(), true);
    assert.equal(await edit.getByRole('textbox', { name: 'Header name', exact: true }).inputValue(), 'Authorization');
    const storedValue = edit.getByLabel('Header value', { exact: true });
    assert.equal(await storedValue.inputValue(), '', 'A stored header value is never sent back to the page');
    assert.equal(await storedValue.getAttribute('placeholder'), 'Leave blank to keep');
    assert.equal(await edit.getByLabel('Ask before running tools', { exact: true }).inputValue(), 'always');
    await edit.getByRole('button', { name: 'Cancel', exact: true }).click();
    await edit.waitFor({ state: 'hidden' });
    await settingsDialog.getByRole('button', { name: 'Close', exact: true }).click();
    await settingsDialog.waitFor({ state: 'hidden' });
    checks.push('A remote HTTP server is added through the same dialog: its header value is posted once, shown afterwards only as a name, its approval policy is kept, and no connection test runs');

    await setCombinedEnabled(true);
    await closeTools();
    await page.getByRole('button', { name: '⚙ Settings', exact: true }).click();
    const settings = page.getByRole('dialog', { name: 'Settings', exact: true });
    await settings.getByRole('button', { name: '语言 / Language', exact: true }).click();
    await settings.getByRole('button', { name: '中文', exact: true }).click();
    await page.getByRole('dialog', { name: '设置', exact: true }).getByRole('button', { name: '关闭', exact: true }).click();
    await page.getByRole('button', { name: '选择工具', exact: true }).click();
    assert.equal(await menu.getByRole('checkbox', { name: '联网搜索', exact: true }).count(), 1);
    assert.equal(await menu.getByRole('checkbox', { name: '读取网页', exact: true }).count(), 0);
    checks.push('Chinese also presents a single 联网搜索 entry, with no duplicate webpage reader');

    // A stored answer whose tool preview was cut offers its complete result, paged from the server.
    const seeded = spawnSync(process.env.LEARNING_TREE_E2E_PYTHON, ['-c', `
import json, os, sys
from pathlib import Path
from sqlalchemy.engine import make_url
from sqlmodel import Session, select
from app.db import engine
from app.models import Message, Node, ToolResult
payload = json.load(sys.stdin)
database = Path(make_url(os.environ['DATABASE_URL']).database).resolve()
assert os.environ['LEARNING_TREE_E2E'] == '1'
assert database.parent.name.startswith('learning-tree-e2e-') and database.name == 'test.db'
with Session(engine) as session:
    node = session.exec(select(Node).where(Node.tree_id == payload['tree']).order_by(Node.id.desc())).first()
    answer = session.exec(select(Message).where(Message.node_id == node.id, Message.role == 'assistant')).first()
    full = 'Synthetic complete tool output. ' * 800 + 'FULL-RESULT-END'
    answer.steps = [{'tool': 'web_search', 'result': full[:3000], 'truncated': True, 'call_index': 1, 'request_id': node.request_id}]
    session.add(answer)
    session.add(ToolResult(node_id=node.id, request_id=node.request_id, call_index=1, tool_name='web_search', content=full))
    session.commit()
    print(json.dumps({'total': len(full)}))
`], { cwd: path.resolve(__dirname, '..'), encoding: 'utf8', input: JSON.stringify({ tree: tree.id }) });
    assert.equal(seeded.status, 0, seeded.stderr);
    const { total } = JSON.parse(seeded.stdout);
    await page.reload(); // The init script restores English.
    await page.locator('.chat-question').filter({ hasText: 'Offline check after disabling a selected web preset.' }).waitFor();
    const storedStep = page.locator('details.chat-reasoning').filter({ hasText: 'Web search · Done' });
    await storedStep.locator('summary').click();
    assert.equal(await storedStep.getByText('FULL-RESULT-END').count(), 0, 'Only the preview is shown at first');
    await storedStep.getByRole('button', { name: 'View full result', exact: true }).click();
    await storedStep.getByRole('button', { name: `Load more (20000 of ${total} characters)`, exact: true }).click();
    await storedStep.getByText(/FULL-RESULT-END/).waitFor();
    assert.equal(await storedStep.getByRole('button').count(), 0, 'Nothing is left to load');
    checks.push('A stored tool step with a cut preview loads its complete result in pages on request');

    const output = path.resolve(__dirname, '../artifacts');
    fs.mkdirSync(output, { recursive: true });
    await page.screenshot({ path: path.join(output, 'web-tools-combined.png'), fullPage: true });
    assert.deepEqual(fixtureUpdates.map(update => update.enabled), [true, false, true]);
    assert.equal(asks.length, 5, 'Every submitted question used the real isolated offline endpoint');
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'web-tools-smoke.json'), JSON.stringify({
      passed: true, checks, provider: 'offline mock', database: 'temporary', mcp: 'browser route fixtures only',
    }, null, 2));
    console.log(JSON.stringify({ passed: true, checks }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
