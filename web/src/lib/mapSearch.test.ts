import { afterEach, describe, expect, it, vi } from "vitest";
import type { SearchHit, TreeNode } from "../types";
import { createMapSearcher, localMatches, mergeSearchResults, plainSnippet, searchAnchor } from "./mapSearch";

function node(id: number, title: string, seed_text: string | null = null): TreeNode {
  return { id, parent_id: id === 1 ? null : 1, tree_id: 7, title, seed_text, has_summary: false };
}
function hit(kind: SearchHit["kind"], node_id: number, snippet: string, message_id: number | null = null, rank = 1): SearchHit {
  return { kind, node_id, message_id, snippet, rank };
}

describe("map search result groups", () => {
  const nodes = [node(1, "快速排序是什么"), node(2, "分支", "pivot 的选择"), node(3, "归并排序")];

  it("matches titles and quotes instantly, case-insensitively", () => {
    expect(localMatches(nodes, " PIVOT ").map(item => item.id)).toEqual([2]);
    expect(localMatches(nodes, "排序").map(item => item.id)).toEqual([1, 3]);
    expect(localMatches(nodes, "   ")).toEqual([]);
  });

  it("keeps server hits as a second group without repeating shown titles or unknown nodes", () => {
    const local = localMatches(nodes, "pivot");
    const hits = [hit("title", 2, "pivot 的选择"), hit("message", 1, "…选一个 pivot…", 12, 2), hit("note", 2, "笔记：pivot", null, 3), hit("message", 99, "deleted elsewhere", 5, 4), hit("message", 1, "…选一个 pivot…", 12, 5)];
    const merged = mergeSearchResults(local, hits, nodes);
    expect(merged.local.map(item => item.id)).toEqual([2]);
    expect(merged.remote.map(item => `${item.kind}:${item.node_id}:${item.message_id}`)).toEqual(["message:1:12", "note:2:null"]);
  });

  it("caps both groups together", () => {
    const many = Array.from({ length: 40 }, (_, index) => node(index + 1, `pivot ${index}`));
    const hits = many.map(item => hit("message", item.id, "pivot", item.id + 100));
    const merged = mergeSearchResults(localMatches(many, "pivot"), hits, many);
    expect(merged.local).toHaveLength(30);
    expect(merged.remote).toHaveLength(0);
    const few = mergeSearchResults(many.slice(0, 28), hits, many);
    expect(few.remote).toHaveLength(2);
  });

  it("anchors only message hits, with a verbatim snippet", () => {
    expect(searchAnchor(hit("message", 1, "…选一个 pivot，把数组分成两边…", 12))).toEqual({ messageId: 12, text: "选一个 pivot，把数组分成两边" });
    expect(searchAnchor(hit("message", 1, "……", 12))).toEqual({ messageId: 12 });
    expect(searchAnchor(hit("note", 2, "笔记"))).toBeUndefined();
    expect(searchAnchor(hit("title", 2, "分支"))).toBeUndefined();
  });

  it("anchors a Markdown answer hit on matching text the rendered answer contains", () => {
    const anchor = (snippet: string, query: string) => searchAnchor(hit("message", 1, snippet, 12), query)?.text;
    expect(anchor("…快速排序**选一个 pivot**，然后\n\n- 把数组分成两边再递归。…", "pivot")).toBe("选一个 pivot");
    expect(anchor("…ally: **median-of-three** keeps the pivot stable…", " Median-of-three ")).toBe("median-of-three");
    expect(anchor("…\n- partition the array around the pivot\n- recurse…", "partition")).toBe("partition the array around the pivot");
    expect(anchor("…\n2. Take a random pivot element.\n…", "random")).toBe("Take a random pivot element.");
    expect(anchor("…use `pivot = arr[0]` here…", "pivot")).toBe("pivot = arr");
    expect(anchor("…see [the pivot rule](https://example.com/pivot) for…", "pivot rule")).toBe("the pivot rule");
    expect(anchor("…first part.\n\nThe pivot is chosen…", "pivot")).toBe("The pivot is chosen");
    expect(anchor("…## Pivot choice\nThe…", "pivot")).toBe("Pivot choice");
    expect(anchor("…> quoted pivot line…", "pivot")).toBe("quoted pivot line");
  });

  it("falls back to the longest plain stretch when the query is not in the snippet verbatim", () => {
    const anchor = (snippet: string, query: string) => searchAnchor(hit("message", 1, snippet, 12), query)?.text;
    expect(anchor("…**the pivôt rule** matters…", "pivot")).toBe("the pivôt rule");
    expect(anchor("…**", "pivot")).toBeUndefined();
  });

  it("shows Markdown snippets as plain text", () => {
    expect(plainSnippet("…快速排序**选一个 pivot**，然后\n\n- 把数组分成两边再递归。…")).toBe("…快速排序选一个 pivot，然后 把数组分成两边再递归。…");
    expect(plainSnippet("…see [the pivot rule](https://example.com/pivot), `code`, _emphasis_, *stress*, but 2*3 and snake_case…")).toBe("…see the pivot rule, code, emphasis, stress, but 2*3 and snake_case…");
    expect(plainSnippet("## Heading\n> quoted\n1. first\n- second ~~old~~")).toBe("Heading quoted first second old");
  });
});

describe("debounced topic search", () => {
  afterEach(() => vi.useRealTimers());

  it("waits for typing to settle and delivers only the latest query", async () => {
    vi.useFakeTimers();
    const queries: string[] = [];
    const search = vi.fn(async (treeId: number, query: string) => { queries.push(`${treeId}:${query}`); return [hit("message", 1, query, 12)]; });
    const delivered: string[][] = [];
    const searcher = createMapSearcher({ search, delayMs: 250 });
    searcher.run(7, "p", hits => delivered.push(hits.map(item => item.snippet)));
    await vi.advanceTimersByTimeAsync(100);
    searcher.run(7, "pi", hits => delivered.push(hits.map(item => item.snippet)));
    await vi.advanceTimersByTimeAsync(100);
    searcher.run(7, "pivot ", hits => delivered.push(hits.map(item => item.snippet)));
    await vi.advanceTimersByTimeAsync(249);
    expect(queries).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    expect(queries).toEqual(["7:pivot"]);
    expect(delivered).toEqual([["pivot"]]);
  });

  it("discards in-flight results when the tree changes or a newer query starts", async () => {
    vi.useFakeTimers();
    let release: (hits: SearchHit[]) => void = () => {};
    const aborted: boolean[] = [];
    const search = vi.fn((_treeId: number, _query: string, signal: AbortSignal) => new Promise<SearchHit[]>(resolve => {
      signal.addEventListener("abort", () => aborted.push(true));
      release = resolve;
    }));
    const delivered: SearchHit[][] = [];
    const searcher = createMapSearcher({ search, delayMs: 10 });
    searcher.run(7, "pivot", hits => delivered.push(hits));
    await vi.advanceTimersByTimeAsync(10);
    expect(search).toHaveBeenCalledTimes(1);
    searcher.cancel();
    expect(aborted).toEqual([true]);
    release([hit("message", 1, "late", 12)]);
    await vi.advanceTimersByTimeAsync(0);
    expect(delivered).toEqual([]);
    searcher.run(8, "pivot", hits => delivered.push(hits));
    await vi.advanceTimersByTimeAsync(10);
    const first = release;
    searcher.run(8, "pivot again", hits => delivered.push(hits));
    await vi.advanceTimersByTimeAsync(10);
    first([hit("message", 1, "stale", 12)]);
    release([hit("message", 2, "fresh", 13)]);
    await vi.advanceTimersByTimeAsync(0);
    expect(delivered.map(hits => hits[0].snippet)).toEqual(["fresh"]);
  });

  it("ignores blank queries and failed requests", async () => {
    vi.useFakeTimers();
    const search = vi.fn(async () => { throw new Error("offline"); });
    const delivered: SearchHit[][] = [];
    const searcher = createMapSearcher({ search, delayMs: 10 });
    searcher.run(7, "   ", hits => delivered.push(hits));
    await vi.advanceTimersByTimeAsync(20);
    expect(search).not.toHaveBeenCalled();
    searcher.run(7, "pivot", hits => delivered.push(hits));
    await vi.advanceTimersByTimeAsync(20);
    expect(search).toHaveBeenCalledTimes(1);
    expect(delivered).toEqual([]);
  });
});
