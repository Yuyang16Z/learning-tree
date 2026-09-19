import { isStreamLost, type AskMeta } from "../api";

interface ResumeOptions {
  /** Reattach after the given event number; throws a StreamLost error while unreachable. */
  follow: (after: number) => Promise<AskMeta>;
  /** Metadata from the dropped stream, including the last event received. */
  lost: AskMeta;
  signal: AbortSignal;
  /** Consecutive failures without progress before giving up. */
  attempts: number;
  onLost?: () => void;
  waitVisible?: (signal: AbortSignal) => Promise<void>;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
}

const aborted = () => new DOMException("Aborted", "AbortError");

/** Resolve once the page is in the foreground; a hidden phone tab cannot use the network. */
export function untilVisible(signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(aborted());
  if (typeof document === "undefined" || document.visibilityState === "visible") return Promise.resolve();
  return new Promise((resolve, reject) => {
    const cleanup = () => { document.removeEventListener("visibilitychange", shown); signal.removeEventListener("abort", abort); };
    const shown = () => { if (document.visibilityState === "visible") { cleanup(); resolve(); } };
    const abort = () => { cleanup(); reject(aborted()); };
    document.addEventListener("visibilitychange", shown);
    signal.addEventListener("abort", abort, { once: true });
  });
}

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, ms);
    const abort = () => { clearTimeout(timer); reject(aborted()); };
    signal.addEventListener("abort", abort, { once: true });
  });
}

/** Keep following an answer across dropped connections, e.g. a phone locking mid-reply. */
export async function resumeAnswer({ follow, lost, signal, attempts, onLost, waitVisible = untilVisible, sleep = pause }: ResumeOptions): Promise<AskMeta> {
  let seq = lost.seq ?? 0;
  let failures = 0;
  for (;;) {
    await waitVisible(signal);
    try {
      return await follow(seq);
    } catch (error) {
      if (signal.aborted || !isStreamLost(error)) throw error;
      const reached = error.meta.seq ?? seq;
      // Receiving more of the answer shows the server is reachable again.
      failures = reached > seq ? 1 : failures + 1;
      seq = reached;
      if (failures >= attempts) throw error;
      onLost?.();
    }
    await sleep(Math.min(8000, 500 * 2 ** (failures - 1)), signal);
  }
}
