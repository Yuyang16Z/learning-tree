import { describe, expect, it, vi } from "vitest";
import type { AskMeta } from "../api";
import { resumeAnswer } from "./answerStream";

const lost = (meta: AskMeta) => Object.assign(new Error("lost"), { meta, lost: true as const });
const immediately = async () => {};
const signal = new AbortController().signal;

describe("reattaching an answer", () => {
  it("continues after the last event received", async () => {
    const follow = vi.fn(async (after: number) => ({ node_id: 5, seq: after + 2, status: "complete" }));
    const meta = await resumeAnswer({ follow, lost: { node_id: 5, seq: 7 }, signal, attempts: Infinity, waitVisible: immediately, sleep: immediately });
    expect(follow).toHaveBeenCalledWith(7);
    expect(meta).toMatchObject({ seq: 9, status: "complete" });
  });

  it("retries with backoff, and any progress resets the retry budget", async () => {
    const outcomes: (AskMeta | Error)[] = [lost({ seq: 3 }), lost({ seq: 3 }), lost({ seq: 5 }), lost({ seq: 5 }), { seq: 9, status: "complete" }];
    const calls: number[] = [];
    const delays: number[] = [];
    const lostCount = vi.fn();
    const meta = await resumeAnswer({
      follow: async after => { calls.push(after); const next = outcomes.shift()!; if (next instanceof Error) throw next; return next; },
      lost: { seq: 3 }, signal, attempts: 3, onLost: lostCount, waitVisible: immediately,
      sleep: async ms => { delays.push(ms); },
    });
    expect(calls).toEqual([3, 3, 3, 5, 5]);
    expect(delays).toEqual([500, 1000, 500, 1000]);
    expect(lostCount).toHaveBeenCalledTimes(4);
    expect(meta.status).toBe("complete");
  });

  it("gives up after consecutive failures without progress", async () => {
    const follow = vi.fn(async () => { throw lost({}); });
    await expect(resumeAnswer({ follow, lost: {}, signal, attempts: 2, waitVisible: immediately, sleep: immediately })).rejects.toMatchObject({ lost: true });
    expect(follow).toHaveBeenCalledTimes(2);
  });

  it("does not retry other failures or a stopped answer", async () => {
    const refused = vi.fn(async () => { throw new Error("服务器没有收到这个问题，请重新发送。"); });
    await expect(resumeAnswer({ follow: refused, lost: {}, signal, attempts: Infinity, waitVisible: immediately, sleep: immediately })).rejects.toThrow("服务器没有收到");
    expect(refused).toHaveBeenCalledTimes(1);

    const controller = new AbortController();
    controller.abort();
    const follow = vi.fn(async () => ({}));
    await expect(resumeAnswer({ follow, lost: {}, signal: controller.signal, attempts: Infinity, sleep: immediately })).rejects.toMatchObject({ name: "AbortError" });
    expect(follow).not.toHaveBeenCalled();
  });
});
