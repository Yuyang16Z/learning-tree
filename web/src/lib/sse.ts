/** Decode framed SSE across arbitrary UTF-8/network boundaries. */
export async function readSSE(body: ReadableStream<Uint8Array>, onEvent: (event: Record<string, any>) => void) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  function dispatch(frame: string) {
    const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).replace(/^ /, '')).join('\n');
    if (data && data !== '[DONE]') onEvent(JSON.parse(data));
  }
  function drain(final = false) {
    let match: RegExpExecArray | null;
    while ((match = /\r?\n\r?\n/.exec(buffer))) {
      dispatch(buffer.slice(0, match.index));
      buffer = buffer.slice(match.index + match[0].length);
    }
    if (final && buffer.trim()) { dispatch(buffer); buffer = ''; }
  }
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) { buffer += decoder.decode(); drain(true); break; }
      buffer += decoder.decode(value, { stream: true });
      drain();
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
