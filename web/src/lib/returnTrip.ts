import type { ReturnSummary } from "../types";

/** A return from a branch to the passage it quoted; kept only in this page, never stored. */
export interface ReturnTrip {
  /** Identifies one return, so a late summary never lands on a newer one. */
  nonce: number;
  treeId: number | null;
  /** The node being left; the summary covers its branch down to this node. */
  fromNodeId: number;
  branchNodeId: number;
  sourceNodeId: number;
  /** Navigation has reached the source node. */
  arrived: boolean;
}

/** Keep a trip while navigation lands on the source; forget it once the learner goes anywhere else. */
export function followReturnTrip(trip: ReturnTrip | null, view: { treeId: number | null; nodeId: number | null }): ReturnTrip | null {
  if (!trip || view.treeId !== trip.treeId) return null;
  if (view.nodeId === trip.sourceNodeId) return trip.arrived ? trip : { ...trip, arrived: true };
  // Before arrival the page still shows the branch being left.
  return !trip.arrived && view.nodeId === trip.fromNodeId ? trip : null;
}

/** The card only appears when the branch left something to show. */
export function hasReturnContent(summary: ReturnSummary | null | undefined): summary is ReturnSummary {
  return !!summary && (summary.excerpts.length > 0 || !!summary.note?.trim() || summary.understanding != null);
}

/** The branch's title, else the start of its quoted passage, else `fallback`. */
export function returnTitle(summary: ReturnSummary, fallback: string, limit = 24): string {
  const title = summary.branch_title.trim();
  if (title) return title;
  const quote = Array.from((summary.source.text ?? "").replace(/\s+/g, " ").trim());
  return quote.length ? quote.slice(0, limit).join("") + (quote.length > limit ? "…" : "") : fallback;
}
