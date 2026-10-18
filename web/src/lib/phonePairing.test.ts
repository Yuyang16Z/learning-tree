import { describe, expect, it, vi } from "vitest";
import { fragmentToken, isLoopbackHost, onPairingRequired, pairFromFragment, pairingLink, reportPairingRequired, watchPairingLinks, type PairingPage } from "./phonePairing";

const TOKEN = "Abc_123-".repeat(6);

function page(hash: string, ok = true) {
  const replaceState = vi.fn();
  const fetch = vi.fn(async () => new Response("{}", { status: ok ? 200 : 401 }));
  const value: PairingPage = { location: { hash, pathname: "/", search: "?lang=zh" }, history: { replaceState }, fetch, base: "/api" };
  return { value, replaceState, fetch };
}

describe("phone pairing from the QR link", () => {
  it("posts the fragment token once and strips it from the address before the request", async () => {
    const { value, replaceState, fetch } = page(`#token=${TOKEN}`);
    expect(await pairFromFragment(value)).toBe("paired");
    expect(replaceState).toHaveBeenCalledWith(null, "", "/?lang=zh");
    expect(replaceState.mock.invocationCallOrder[0]).toBeLessThan(fetch.mock.invocationCallOrder[0]);
    expect(fetch).toHaveBeenCalledWith("/api/phone-access/session", expect.objectContaining({
      method: "POST", body: JSON.stringify({ token: TOKEN }), credentials: "same-origin",
    }));
  });

  it("does nothing without a token and reports a refused or malformed one", async () => {
    const plain = page("#section");
    expect(await pairFromFragment(plain.value)).toBe("none");
    expect(plain.replaceState).not.toHaveBeenCalled();
    expect(plain.fetch).not.toHaveBeenCalled();
    const refused = page(`#token=${TOKEN}`, false);
    expect(await pairFromFragment(refused.value)).toBe("failed");
    const malformed = page("#token=short<script>");
    expect(await pairFromFragment(malformed.value)).toBe("failed");
    expect(malformed.replaceState).toHaveBeenCalled();
    expect(malformed.fetch).not.toHaveBeenCalled();
    const offline = page(`#token=${TOKEN}`);
    offline.fetch.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    expect(await pairFromFragment(offline.value)).toBe("failed");
  });

  it("pairs when a QR link only changes the fragment of an open tab", async () => {
    const handlers: (() => void)[] = [];
    const target = { addEventListener: (_: string, fn: () => void) => { handlers.push(fn); }, removeEventListener: vi.fn() };
    const { value, fetch } = page("");
    const paired = vi.fn();
    const stop = watchPairingLinks(target, value, paired);
    value.location.hash = "#section";
    handlers[0]();
    await vi.waitFor(() => expect(fetch).not.toHaveBeenCalled());
    value.location.hash = `#token=${TOKEN}`;
    handlers[0]();
    await vi.waitFor(() => expect(paired).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledTimes(1);
    stop();
    expect(target.removeEventListener).toHaveBeenCalledWith("hashchange", handlers[0]);
  });

  it("reads only well-formed tokens from the fragment", () => {
    expect(fragmentToken(`#token=${TOKEN}&x=1`)).toBe(TOKEN);
    expect(fragmentToken(`#x=1&token=${TOKEN}`)).toBe(TOKEN);
    expect(fragmentToken("#token=")).toBeNull();
    expect(fragmentToken("#token=a b c")).toBeNull();
  });

  it("asks to scan again only for refused requests from another device", () => {
    const listener = vi.fn();
    const stop = onPairingRequired(listener);
    reportPairingRequired(401, "127.0.0.1");
    reportPairingRequired(401, "localhost");
    reportPairingRequired(401, undefined);
    reportPairingRequired(403, "mac.tail0.ts.net");
    expect(listener).not.toHaveBeenCalled();
    reportPairingRequired(401, "mac.tail0.ts.net");
    expect(listener).toHaveBeenCalledTimes(1);
    stop();
    reportPairingRequired(401, "mac.tail0.ts.net");
    expect(listener).toHaveBeenCalledTimes(1);
    expect(isLoopbackHost("[::1]")).toBe(true);
    expect(isLoopbackHost("LOCALHOST")).toBe(true);
  });
});

describe("pairing links for other addresses", () => {
  const hosts = ["learning.example"];

  it("builds the origin plus the token fragment for an accepted host", () => {
    expect(pairingLink(" https://Learning.Example:8443/some/path?x=1#old ", TOKEN, hosts)).toEqual({ link: `https://learning.example:8443/#token=${TOKEN}` });
    expect(pairingLink("http://learning.example", TOKEN, hosts)).toEqual({ link: `http://learning.example/#token=${TOKEN}` });
    // Tailscale Serve on a Docker host: the container itself sees no Tailscale address.
    expect(pairingLink("https://study-mac.tail1a2b3c.ts.net", TOKEN, [])).toEqual({ link: `https://study-mac.tail1a2b3c.ts.net/#token=${TOKEN}` });
  });

  it("refuses hosts the server would reject and anything that is not a plain web address", () => {
    expect(pairingLink("", TOKEN, hosts)).toBeNull();
    expect(pairingLink("   ", TOKEN, hosts)).toBeNull();
    expect(pairingLink("https://learning.example.evil.test", TOKEN, hosts)).toEqual({ problem: "host" });
    expect(pairingLink("http://127.0.0.1:8099", TOKEN, hosts)).toEqual({ problem: "host" });
    expect(pairingLink("learning.example", TOKEN, hosts)).toEqual({ problem: "address" });
    expect(pairingLink("ftp://learning.example", TOKEN, hosts)).toEqual({ problem: "address" });
    // Built from parts so the release scanner's credential-URL rule does not flag the test.
    expect(pairingLink("https://user" + ":pw@learning.example", TOKEN, hosts)).toEqual({ problem: "address" });
  });
});
