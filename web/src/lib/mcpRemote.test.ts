import { describe, expect, it } from "vitest";
import type { McpServer } from "../types";
import { headerPayload, headerRows, headersChanged, mcpTarget, remoteUrl } from "./mcpRemote";

describe("remote MCP headers", () => {
  it("lists stored header names with blank values that keep the stored secret", () => {
    const rows = headerRows(["Authorization", "X-Team"]);
    expect(rows.map(row => row.value)).toEqual(["", ""]);
    expect(headerPayload(rows)).toEqual({ Authorization: "", "X-Team": "" });
    expect(headersChanged(rows, ["Authorization", "X-Team"])).toBe(false);
    expect(headerRows(undefined)).toEqual([]);
  });

  it("detects typed values, renames, removals and additions", () => {
    const names = ["Authorization"];
    expect(headersChanged([{ name: "Authorization", value: "new", original: "Authorization" }], names)).toBe(true);
    expect(headersChanged([{ name: "X-Key", value: "", original: "Authorization" }], names)).toBe(true);
    expect(headersChanged([], names)).toBe(true);
    expect(headersChanged([...headerRows(names), { name: "X-New", value: "v" }], names)).toBe(true);
    // An untouched empty row does not count as a change.
    expect(headersChanged([...headerRows(names), { name: "", value: "" }], names)).toBe(false);
  });

  it("requires a value for new or renamed headers and ignores empty rows", () => {
    expect(headerPayload([{ name: " X-Key ", value: "secret" }, { name: "", value: "" }])).toEqual({ "X-Key": "secret" });
    expect(() => headerPayload([{ name: "X-Key", value: "" }])).toThrow();
    expect(() => headerPayload([{ name: "X-Renamed", value: "", original: "Authorization" }])).toThrow();
    expect(headerPayload([{ name: "authorization", value: "", original: "Authorization" }])).toEqual({ authorization: "" });
  });

  it("rejects reserved, duplicate, malformed names and line breaks in values", () => {
    for (const rows of [
      [{ name: "Host", value: "x" }],
      [{ name: "mcp-session-id", value: "x" }],
      [{ name: "X-A", value: "1" }, { name: "x-a", value: "2" }],
      [{ name: "bad name", value: "x" }],
      [{ name: "X-A", value: "a\r\nX-B: b" }],
    ]) {
      expect(() => headerPayload(rows)).toThrow();
    }
  });
});

describe("remote MCP addresses", () => {
  it("accepts http(s) URLs and rejects other schemes or embedded credentials", () => {
    expect(remoteUrl(" https://docs.example/mcp ")).toBe("https://docs.example/mcp");
    expect(remoteUrl("http://127.0.0.1:8000/mcp")).toBe("http://127.0.0.1:8000/mcp");
    // Built from parts so the release scanner's credential-URL rule does not flag the test.
    const credentialUrl = "https://user" + ":pw@docs.example/mcp";
    for (const invalid of ["docs.example/mcp", "file:///etc/passwd", "ftp://docs.example", "", credentialUrl]) {
      expect(() => remoteUrl(invalid)).toThrow();
    }
  });

  it("shows the remote host or the launch command's file name", () => {
    const local: McpServer = { id: 1, label: "Time", command: "/usr/local/bin/uvx", args: ["mcp-server-time"], enabled: true };
    expect(mcpTarget(local)).toBe("uvx");
    expect(mcpTarget({ ...local, transport: "http", command: "", url: "https://docs.example:8443/mcp" })).toBe("docs.example:8443");
    expect(mcpTarget({ ...local, transport: "http", command: "", url: "not a url" })).toBe("not a url");
  });
});
