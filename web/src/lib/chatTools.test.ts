import { describe, expect, it } from "vitest";
import type { McpServer } from "../types";
import { hasManagedWebSearch, managedMcpPreset, selectedChatTools } from "./chatTools";

const web: McpServer = {
  id: 3, label: "联网搜索", command: "/usr/bin/python3",
  args: ["/work/My Project/integrations/mcp/launch.py", "web-research"], enabled: true,
};
const files: McpServer = { ...web, id: 4, label: "学习资料", args: [web.args[0], "filesystem"] };
const builtins = { useSearch: true, useFetch: true, selectedMcp: [] };

describe("chat tool authorization", () => {
  it("recognizes only an enabled managed web-research preset, including renamed presets", () => {
    expect(hasManagedWebSearch([web])).toBe(true);
    expect(hasManagedWebSearch([{ ...web, label: "My research" }])).toBe(true);
    expect(hasManagedWebSearch([{ ...web, enabled: false }])).toBe(false);
    expect(hasManagedWebSearch([{ ...web, args: ["/custom/server.py", "web-research"] }])).toBe(false);
    expect(hasManagedWebSearch([{ ...web, args: [web.args[0], "fetch"] }])).toBe(false);
    expect(hasManagedWebSearch([{ ...web, args: [] }])).toBe(false);
    expect(managedMcpPreset({ ...web, args: ["C:\\Learning Tree\\integrations\\mcp\\launch.py", "web-research"] })).toBe("web-research");
  });

  it("suppresses stale builtin selections without automatically selecting the combined MCP", () => {
    expect(selectedChatTools([web, files], builtins)).toEqual([]);
    expect(selectedChatTools([web, files], { ...builtins, selectedMcp: [web.id] })).toEqual(["mcp_server_3"]);
    expect(selectedChatTools([web, files], { ...builtins, selectedMcp: [files.id] })).toEqual(["mcp_server_4"]);
  });

  it("retains builtin compatibility when the combined MCP is absent or disabled", () => {
    expect(selectedChatTools([], builtins)).toEqual(["web_search", "fetch"]);
    expect(selectedChatTools([files], builtins)).toEqual(["web_search", "fetch"]);
    expect(selectedChatTools([{ ...web, enabled: false }], builtins)).toEqual(["web_search", "fetch"]);
    expect(selectedChatTools([{ ...web, args: ["/custom/server.py", "web-research"] }], builtins)).toEqual(["web_search", "fetch"]);
  });

  it("never sends unchecked, disabled, missing or duplicate MCP permissions", () => {
    const selection = { useSearch: false, useFetch: false, selectedMcp: [3, 3, 4, 99] };
    expect(selectedChatTools([web, { ...files, enabled: false }], selection)).toEqual(["mcp_server_3"]);
    expect(selectedChatTools([{ ...web, enabled: false }], selection)).toEqual([]);
    expect(selectedChatTools([web], { ...selection, selectedMcp: [] })).toEqual([]);
    expect(selection.selectedMcp).toEqual([3, 3, 4, 99]);
  });
});
