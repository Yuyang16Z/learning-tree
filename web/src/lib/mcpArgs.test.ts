import { describe, expect, it } from "vitest";
import { parseMcpArgs } from "./mcpArgs";

describe("MCP argument editing", () => {
  it("round-trips Chinese paths, spaces, quotes, and empty arguments without shell splitting", () => {
    const args = ["--directory", "/workspace/学习 资料", 'a "quoted" value', ""];
    expect(parseMcpArgs(JSON.stringify(args, null, 2))).toEqual(args);
  });

  it("accepts an empty argument list", () => {
    expect(parseMcpArgs("[]")).toEqual([]);
    expect(parseMcpArgs(" ")).toEqual([]);
  });

  it("rejects shell syntax and non-string arguments before saving", () => {
    for (const invalid of ["--directory /tmp", '["valid", 1]', '{"arg":"value"}', "null"]) {
      expect(() => parseMcpArgs(invalid)).toThrow();
    }
  });
});
