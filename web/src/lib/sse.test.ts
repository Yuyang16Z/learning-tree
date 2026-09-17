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
