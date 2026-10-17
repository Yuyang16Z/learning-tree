/** Phone pairing: the QR link's "#token=…" becomes a same-site cookie before the app loads. */

export interface PairingPage {
  location: { hash: string; pathname: string; search: string };
  history: { replaceState(data: unknown, unused: string, url?: string | URL | null): void };
  fetch: (input: string, init?: RequestInit) => Promise<Response>;
  base: string;
}
export type PairingResult = "none" | "paired" | "failed";

const LOOPBACK = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

export function isLoopbackHost(hostname: string): boolean {
  return LOOPBACK.has(hostname.toLowerCase());
}

export function fragmentToken(hash: string): string | null {
  const value = new URLSearchParams(hash.replace(/^#/, "")).get("token");
  return value && /^[A-Za-z0-9_-]{32,200}$/.test(value) ? value : null;
}

/** Pair this browser from a "#token=…" link, removing the token from the address first. */
export async function pairFromFragment(page: PairingPage): Promise<PairingResult> {
  if (!/(?:^#|&)token=/.test(page.location.hash)) return "none";
  const token = fragmentToken(page.location.hash);
  // The token must not stay in the address bar, the history or a bookmark.
  page.history.replaceState(null, "", page.location.pathname + page.location.search);
  if (!token) return "failed";
  try {
    const response = await page.fetch(`${page.base}/phone-access/session`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token }),
      credentials: "same-origin",
    });
    return response.ok ? "paired" : "failed";
  } catch {
    return "failed";
  }
}

/**
 * A QR link opened in a tab that already shows the app may only change its fragment,
 * which reloads nothing; pair on that change too. Returns a function that stops watching.
 */
export function watchPairingLinks(
  target: { addEventListener(type: "hashchange", listener: () => void): void; removeEventListener(type: "hashchange", listener: () => void): void },
  page: PairingPage,
  onPaired: () => void,
): () => void {
  const listener = () => { void pairFromFragment(page).then(result => { if (result === "paired") onPaired(); }); };
  target.addEventListener("hashchange", listener);
  return () => target.removeEventListener("hashchange", listener);
}

const listeners = new Set<() => void>();

/** Called when another device's request is refused for missing pairing; returns an unsubscribe. */
export function onPairingRequired(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/** A 401 means the access check refused this browser; on this computer it never applies. */
export function reportPairingRequired(status: number, hostname: string | undefined): void {
  if (status !== 401 || hostname === undefined || isLoopbackHost(hostname)) return;
  for (const listener of [...listeners]) listener();
}
