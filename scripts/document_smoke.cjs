// Synthetic documents only. manage.py e2e owns the temporary database and server.
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

async function poll(callback, label) {
  for (let i = 0; i < 150; i++) {
    const result = await callback();
    if (result) return result;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${label}`);
}

const file = (name, text) => ({ name, mimeType: 'text/plain', buffer: Buffer.from(text, 'utf8') });
const note = file('learning-note.md', '# Synthetic learning note\n\nThe sample marker is ORCHID-417.\n\nThis generated fixture contains no personal information.\n');
const image = { name: 'learning-diagram.png', mimeType: 'image/png', buffer: Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9H0AAAAASUVORK5CYII=', 'base64') };
const imageDataUrl = `data:${image.mimeType};base64,${image.buffer.toString('base64')}`;

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base });
  const page = await context.newPage();
  const checks = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === base) return route.continue();
    errors.push('Unexpected external browser request');
    return route.abort();
  });
  try {
    const models = await api('/models');
    assert.equal(models.length, 1);
    assert.equal(models[0].llm_model, 'mock');
    const tree = await api('/trees', { title: 'Synthetic document upload checks' });
    await context.addInitScript(({ treeId }) => {
      localStorage.setItem('learning-tree.locale', 'en');
      localStorage.setItem('bl-tree', JSON.stringify(treeId));
    }, { treeId: tree.id });
    await page.goto(base);
    await page.getByRole('heading', { name: tree.title, exact: true, level: 1 }).waitFor();
    const composer = page.locator('.chat-compose-box');
    const input = composer.locator('textarea');
    const upload = page.getByTestId('document-upload-input');
    const cards = scope => scope.locator('.chat-document-card');
    const card = (scope, name) => cards(scope).filter({ has: page.locator('.chat-document-name', { hasText: name }) });
    const uploadResponse = () => page.waitForResponse(response =>
      /\/api\/nodes\/\d+\/documents$/.test(response.url()) && response.request().method() === 'POST');
    const askRequest = () => page.waitForRequest(request =>
      /\/api\/nodes\/\d+\/ask$/.test(request.url()) && request.method() === 'POST');
    async function uploadFile(fixture) {
      const pending = uploadResponse();
      await upload.setInputFiles(fixture);
      const response = await pending;
      assert(response.ok(), `Upload ${fixture.name}: ${response.status()}`);
      const document = await response.json();
      await card(composer, fixture.name).waitFor();
      return document;
    }
    async function completed(question) {
      return poll(async () => {
        const nodes = await api(`/trees/${tree.id}`);
        for (const node of nodes) {
          if (node.status !== 'complete') continue;
          const detail = await api(`/nodes/${node.id}`);
          if (detail.messages.some(message => message.role === 'user' && message.content === question)) return detail;
        }
      }, `completed answer to ${question}`);
    }
    const ids = node => node.messages.filter(message => message.role === 'user').flatMap(message => (message.documents ?? []).map(document => document.id));

    assert.equal(await composer.getByRole('button', { name: 'Add attachments', exact: true }).count(), 1);
    assert.equal(await composer.getByRole('button', { name: /^(Add images|Add documents)$/ }).count(), 0);
    assert.equal(await composer.locator('input[type="file"]').count(), 1);
    assert.match(await upload.getAttribute('accept'), /image\/\*/);
    assert.match(await upload.getAttribute('accept'), /\.pdf/);
    checks.push('One attachment button and picker accept both images and documents');

    await uploadFile(file('discard-me.txt', 'Generated draft attachment to remove.'));
    await composer.getByRole('button', { name: 'Remove document discard-me.txt', exact: true }).click();
    await poll(async () => await cards(composer).count() === 0, 'removed draft document');
    checks.push('A parsed document can be removed from an unsent draft');

    // Hold only this local response to observe upload state without adding timing sleeps.
    let releaseUpload;
    const uploadGate = new Promise(resolve => { releaseUpload = resolve; });
    let enteredUpload;
    const uploadEntered = new Promise(resolve => { enteredUpload = resolve; });
    const uploadRoute = /\/api\/nodes\/\d+\/documents$/;
    await page.route(uploadRoute, async route => {
      if (route.request().method() !== 'POST') return route.continue();
      const response = await route.fetch();
      enteredUpload();
      await uploadGate;
      await route.fulfill({ response });
    });
    const pendingUpload = uploadResponse();
    await input.fill('What does the attached learning note say?');
    const attachmentsChooserReady = page.waitForEvent('filechooser');
    await composer.getByRole('button', { name: 'Add attachments', exact: true }).click();
    const attachmentsChooser = await attachmentsChooserReady;
    assert(attachmentsChooser.isMultiple());
    await attachmentsChooser.setFiles([image, note]);
    await uploadEntered;
    const pendingImage = composer.locator('.chat-pending-images img');
    await pendingImage.waitFor();
    assert.equal(await pendingImage.getAttribute('src'), imageDataUrl);
    assert.equal(await composer.locator('.chat-document-upload .chat-document-name').innerText(), note.name);
    await composer.locator('.chat-document-upload').waitFor();
    assert.equal(await composer.getByRole('button', { name: 'Send question', exact: true }).isDisabled(), true);
    releaseUpload();
    const documentResponse = await pendingUpload;
    assert(documentResponse.ok());
    const document = await documentResponse.json();
    await card(composer, note.name).waitFor();
    await page.unroute(uploadRoute);
    assert(document.characters > 0);
    checks.push('Upload state blocks sending until the actual local parser returns a document');
    checks.push('A mixed picker selection shows an image preview and a separately parsed document');

    await card(composer, note.name).getByRole('button', { name: 'Preview text', exact: true }).click();
    await poll(async () => (await page.locator('.chat-document-preview').innerText()).includes('ORCHID-417'), 'parsed text preview');
    const downloadReady = page.waitForEvent('download');
    await card(composer, note.name).getByText('Download', { exact: true }).click();
    const download = await downloadReady;
    assert.equal(download.suggestedFilename(), note.name);
    assert.deepEqual(fs.readFileSync(await download.path()), note.buffer);
    checks.push('Preview shows parsed content and downloading returns the original synthetic bytes');

    await page.reload();
    await input.waitFor();
    await card(composer, note.name).waitFor();
    await pendingImage.waitFor();
    assert.equal(await pendingImage.getAttribute('src'), imageDataUrl);
    assert.equal(await input.inputValue(), 'What does the attached learning note say?');
    const firstRequest = askRequest();
    await composer.getByRole('button', { name: 'Send question', exact: true }).click();
    const firstBody = (await firstRequest).postDataJSON();
    assert.deepEqual(firstBody.document_ids, [document.id]);
    assert.deepEqual(firstBody.images, [imageDataUrl]);
    const first = await completed('What does the attached learning note say?');
    assert.equal(first.id, tree.root_node_id);
    assert.deepEqual(ids(first), [document.id]);
    assert.deepEqual(first.messages.find(message => message.role === 'user').images, [imageDataUrl]);
    const firstTurn = page.locator(`[data-turn-node="${first.id}"]`);
    await card(firstTurn, note.name).waitFor();
    await firstTurn.locator('.chat-question .chat-images img').waitFor();
    assert.equal(await firstTurn.locator('.chat-question .chat-images img').getAttribute('src'), imageDataUrl);
    await poll(async () => await cards(composer).count() === 0, 'accepted attachment cleared from composer');
    assert.equal(await pendingImage.count(), 0);
    checks.push('A draft survives reload; sending binds the parsed document to the saved question and clears the draft');
    checks.push('A mixed image and document draft survives reload and both attachments reach the saved question');

    const storedAnswer = first.messages.find(message => message.role === 'assistant').content;
    await firstTurn.getByRole('button', { name: 'Copy answer', exact: true }).click();
    await firstTurn.locator('.chat-answer-actions').getByRole('button', { name: 'Copied', exact: true }).waitFor();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), storedAnswer);
    await firstTurn.locator('.chat-question').hover();
    await firstTurn.getByRole('button', { name: 'Copy question', exact: true }).click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'What does the attached learning note say?');
    checks.push('Copy answer writes exact saved Markdown to the actual clipboard; question copy writes only its text');

    await page.reload();
    await card(firstTurn, note.name).waitFor();
    await input.fill('Continue explaining the same note.');
    await composer.getByRole('button', { name: 'Send question', exact: true }).click();
    const followup = await completed('Continue explaining the same note.');
    const thread = await api(`/nodes/${followup.id}/thread`);
    assert(thread.nodes.some(node => node.node_id === first.id && node.documents.some(item => item.id === document.id)));
    await card(firstTurn, note.name).waitFor();
    checks.push('Sent document cards survive reload and remain on the visible ancestor path during follow-ups');

    await firstTurn.locator('.chat-question').hover();
    await firstTurn.getByRole('button', { name: 'Edit', exact: true }).click();
    await composer.locator('.chat-revision').waitFor();
    await card(composer, note.name).waitFor();
    await input.fill('Give a shorter explanation of the attached note.');
    const revisionRequest = askRequest();
    await composer.getByRole('button', { name: 'Send question', exact: true }).click();
    const revisionBody = (await revisionRequest).postDataJSON();
    assert.equal(revisionBody.mode, 'revise');
    assert.deepEqual(revisionBody.document_ids, [document.id]);
    const revision = await completed('Give a shorter explanation of the attached note.');
    assert.equal(revision.revision_of, first.id);
    assert.deepEqual(ids(revision), [document.id]);
    assert.deepEqual(ids(await api(`/nodes/${first.id}`)), [document.id]);
    checks.push('Editing carries document references into a new version and preserves the original question');

    const exportReady = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Export learning history', exact: true }).click();
    const exported = await exportReady;
    const backupBytes = fs.readFileSync(await exported.path());
    const backup = JSON.parse(backupBytes.toString('utf8'));
    assert.equal(backup.version, 2);
    assert(backup.documents.some(item => item.id === document.id));
    const importReady = page.waitForResponse(response => response.url() === base + '/api/trees/import');
    const chooserReady = page.waitForEvent('filechooser');
    await page.getByRole('button', { name: '↥ Import learning records', exact: true }).click();
    await (await chooserReady).setFiles({ name: 'synthetic-document-backup.json', mimeType: 'application/json', buffer: backupBytes });
    const importResponse = await importReady;
    assert(importResponse.ok(), `Import: ${importResponse.status()}`);
    const imported = await importResponse.json();
    assert.notEqual(imported.id, tree.id);
    const importedRoot = await api(`/nodes/${imported.root_node_id}`);
    const importedDocuments = importedRoot.messages.filter(message => message.role === 'user').flatMap(message => message.documents ?? []);
    assert.equal(importedDocuments.length, 1);
    assert.notEqual(importedDocuments[0].id, document.id);
    assert.equal(importedDocuments[0].name, note.name);
    const importedTurn = page.locator(`[data-turn-node="${imported.root_node_id}"]`);
    await card(importedTurn, note.name).waitFor();
    const importedDownloadReady = page.waitForEvent('download');
    await card(importedTurn, note.name).getByText('Download', { exact: true }).click();
    const importedDownload = await importedDownloadReady;
    assert.deepEqual(fs.readFileSync(await importedDownload.path()), note.buffer);
    assert.deepEqual(ids(await api(`/nodes/${first.id}`)), [document.id]);
    checks.push('UI export and import retain document originals, remap references and leave the source tree unchanged');

    const output = path.resolve(__dirname, '../artifacts');
    fs.mkdirSync(output, { recursive: true });
    await page.screenshot({ path: path.join(output, 'document-smoke.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    async function usableNarrowComposer() {
      await page.locator('.learning-map').waitFor({ state: 'detached' });
      await composer.waitFor({ state: 'visible' });
      await input.waitFor({ state: 'visible' });
      const composerBox = await composer.boundingBox();
      const inputBox = await input.boundingBox();
      assert(composerBox && composerBox.width > 250, 'The composer must retain usable width');
      assert(inputBox && inputBox.width > 250, 'The text input must retain usable width');
      await input.fill('The narrow-screen composer remains usable.');
      assert.equal(await input.inputValue(), 'The narrow-screen composer remains usable.');
      await input.fill('');
    }
    await usableNarrowComposer();
    await page.getByRole('button', { name: 'Open topic list', exact: true }).click();
    await page.locator('.sidebar-scrim').waitFor();
    await page.locator('.sidebar .topic-select').filter({ hasText: tree.title }).first().click();
    await page.locator('.sidebar-scrim').waitFor({ state: 'detached' });
    await poll(async () => (await page.evaluate(() => JSON.parse(localStorage.getItem('bl-tree')))) === tree.id, 'topic selected from narrow sidebar');
    await usableNarrowComposer();
    checks.push('At 390px, the chat input remains wider than 250px and selecting a topic closes the overlay');

    await page.getByRole('button', { name: 'Show learning tree', exact: true }).click();
    await page.locator('.learning-map').waitFor({ state: 'visible' });
    await page.locator('.map-scrim').click({ position: { x: 10, y: 200 } });
    await usableNarrowComposer();
    await card(page.locator('.chat-messages'), note.name).first().waitFor({ state: 'visible' });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    checks.push('At 390px, opening and dismissing the learning map restores a usable chat and visible document cards without overflow');
    await page.screenshot({ path: path.join(output, 'document-smoke-narrow.png'), fullPage: true });
    fs.writeFileSync(path.join(output, 'document-smoke.json'), JSON.stringify({
      passed: true, checks, provider: 'offline mock', database: 'temporary', documents: 'generated synthetic text',
      images: 'generated one-pixel PNG', clipboard: 'actual browser clipboard',
      answer_quality_evaluated: false,
    }, null, 2));
    console.log(JSON.stringify({ passed: true, checks }));
  } finally {
    await context.close();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
