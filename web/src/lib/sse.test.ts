import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readSSE } from './sse';
import { api } from '../api';
import { NavigationGate } from './workspace';
function stream(text: string, step = 1) {
  const bytes = new TextEncoder().encode(text);
  return new ReadableStream<Uint8Array>({ start(controller) {
    for (let i = 0; i < bytes.length; i += step) controller.enqueue(bytes.slice(i, i + step));
    controller.close();
  } });
}
describe('stream recovery boundaries', () => {
  beforeEach(() => { vi.stubGlobal('navigator', { languages: ['zh-CN'], language: 'zh-CN' }); });
  afterEach(() => { vi.unstubAllGlobals(); });
  it('decodes Chinese split across bytes, CRLF and an unframed final event', async () => {
    const out: unknown[] = [];
    await readSSE(stream(': comment\r\ndata: {"delta":"你好🌱"}\r\n\r\ndata: {"done":true}'), event => out.push(event));
    expect(out).toEqual([{ delta: '你好🌱' }, { done: true }]);
  });
  it.each([['zh-CN', '连接中断'], ['en-US', 'The connection was interrupted']])('requires terminal success and preserves recovery metadata in %s', async (language, message) => {
    vi.stubGlobal('navigator', { languages: [language], language });
    vi.stubGlobal('fetch', vi.fn(async () => new Response(stream('data: {"started":true,"node_id":9}\n\ndata: {"delta":"半截"}\n\n'))));
    const start = vi.fn(); const delta = vi.fn();
    await expect(api.ask(1, { question: '问题', config_id: 1 }, { onStart: start, onDelta: delta })).rejects.toMatchObject({ message: expect.stringContaining(message), meta: expect.objectContaining({ node_id: 9 }) });
    expect(start).toHaveBeenCalledWith(expect.objectContaining({ node_id: 9 }));
    expect(delta).toHaveBeenCalledWith('半截');
  });
  it('keeps failure metadata even if a server incorrectly sends done', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(stream('data: {"error":"供应商暂时不可用","node_id":7}\n\ndata: {"done":true}\n\n'))));
    await expect(api.ask(1, { question: '问题', config_id: 1 }, { onDelta() {} })).rejects.toMatchObject({ message: '供应商暂时不可用', meta: { node_id: 7 } });
  });
  it('succeeds only with done and releases the stream lock', async () => {
    const body = stream('data: {"delta":"完整"}\n\ndata: {"done":true,"node_id":2}\n\n', 5);
    vi.stubGlobal('fetch', vi.fn(async () => new Response(body)));
    expect(await api.ask(1, { question: '问题', config_id: 1 }, { onDelta() {} })).toMatchObject({ node_id: 2 });
    expect(body.locked).toBe(false);
  });
  it('dispatches only valid context states in order without treating them as answer content', async () => {
    const events = [
      { started: true, node_id: 9 },
      { context_status: 'summarizing' },
      { context_status: 'unknown', summary: 'private intermediate text' },
      { context_status: { status: 'ready' } },
      { context_status: null },
      { context_status: 'ready' },
      { reasoning: 'Reasoning' },
      { delta: '回答' },
      { done: true, node_id: 9 },
    ];
    vi.stubGlobal('fetch', vi.fn(async () => new Response(stream(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('')))));
    const received: string[] = [];
    const result = await api.ask(1, { question: '问题', config_id: 1 }, {
      onStart: () => received.push('started'),
      onContextStatus: status => received.push(status),
      onReasoning: value => received.push(`reasoning:${value}`),
      onDelta: value => received.push(`answer:${value}`),
    });
    expect(received).toEqual(['started', 'summarizing', 'ready', 'reasoning:Reasoning', 'answer:回答']);
    expect(result).not.toHaveProperty('context_status');
    expect(result).not.toHaveProperty('summary');
  });
  it('hands numeric diagnostics to their handler without leaking them into the result', async () => {
    const report = { estimated_input_tokens: 1234, actual_input_tokens: 1100, cached_input_tokens: 800, output_tokens: 42, compacted: false, tool_rounds: 2, calibration_ratio: 1, timings_ms: { retrieval: 10, tools: 20, summary: 0, model: 300, total: 400 } };
    const events = [
      { started: true, node_id: 9 },
      { diagnostics: 'not an object' },
      { diagnostics: [report] },
      { diagnostics: report, seq: 2 },
      { delta: '回答' },
      { done: true, node_id: 9 },
    ];
    vi.stubGlobal('fetch', vi.fn(async () => new Response(stream(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('')))));
    const diagnostics = vi.fn();
    const result = await api.ask(1, { question: '问题', config_id: 1 }, { onDelta() {}, onDiagnostics: diagnostics });
    expect(diagnostics.mock.calls).toEqual([[report]]);
    expect(result).not.toHaveProperty('diagnostics');
    expect(result).toMatchObject({ node_id: 9, seq: 2 });
  });
  it('dispatches memory sources once without merging them into the result', async () => {
    const sources = [{ memory_id: 7, source_node_id: 1, tree_id: 1, tree_title: '当前树', scope: 'path' }];
    const events = [{ started: true, node_id: 9 }, { sources }, { sources: 'not a list' }, { delta: '回答' }, { done: true, node_id: 9 }];
    vi.stubGlobal('fetch', vi.fn(async () => new Response(stream(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('')))));
    const received = vi.fn();
    const result = await api.ask(1, { question: '问题', config_id: 1 }, { onDelta() {}, onSources: received });
    expect(received.mock.calls).toEqual([[sources]]);
    expect(result).not.toHaveProperty('sources');
  });
  it('hands tool results their stored-result reference from the event and its envelope', async () => {
    const events = [
      { started: true, node_id: 9, request_id: 'r1' },
      { tool_start: { name: 'web_search', args: { query: 'x' } }, seq: 1, node_id: 9, request_id: 'r1' },
      { tool_end: { name: 'web_search', result: 'preview', call_index: 1, truncated: true }, seq: 2, node_id: 9, request_id: 'r1' },
      { tool_end: { name: 'fetch', result: 'short', truncated: false }, seq: 3, node_id: 9, request_id: 'r1' },
      { delta: '回答' },
      { done: true, node_id: 9 },
    ];
    vi.stubGlobal('fetch', vi.fn(async () => new Response(stream(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('')))));
    const starts = vi.fn(); const ends = vi.fn();
    const result = await api.ask(1, { question: '问题', config_id: 1 }, { onDelta() {}, onToolStart: starts, onToolEnd: ends });
    expect(starts.mock.calls).toEqual([['web_search', { query: 'x' }]]);
    expect(ends.mock.calls).toEqual([
      ['web_search', 'preview', { call_index: 1, truncated: true, request_id: 'r1', node_id: 9 }],
      ['fetch', 'short', { truncated: false }],
    ]);
    expect(result).not.toHaveProperty('tool_end');
  });
  it('does not mistake context readiness for terminal success', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(stream('data: {"context_status":"summarizing"}\n\ndata: {"context_status":"ready"}\n\n'))));
    const contextStatus = vi.fn();
    await expect(api.ask(1, { question: '问题', config_id: 1 }, { onDelta() {}, onContextStatus: contextStatus })).rejects.toThrow('连接中断');
    expect(contextStatus.mock.calls).toEqual([['summarizing'], ['ready']]);
  });
  it('invalidates old requests even when returning to the same tree', () => {
    const gate = new NavigationGate(); const a = gate.next(); const b = gate.next(); const c = gate.next();
    expect(gate.current(a)).toBe(false); expect(gate.current(b)).toBe(false); expect(gate.current(c)).toBe(true);
  });
});
