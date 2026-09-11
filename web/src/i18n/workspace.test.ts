import { describe, expect, it } from "vitest";
import { localizeError, mcpDisplayName } from "./workspace";
import type { McpServer } from "../types";

const server: McpServer = { id: 1, label: "学习资料", command: "/usr/bin/python3", args: ["/work/My Project/integrations/mcp/launch.py", "filesystem"], enabled: true };

describe("localized application diagnostics", () => {
  it("localizes known backend errors, test status, and HTTP diagnostics", () => {
    expect(localizeError("请先在设置中添加模型", "en")).toBe("Add a model in Settings first");
    expect(localizeError("HTTP 401：API Key 无效，请检查密钥。", "en")).toBe("HTTP 401: Invalid API key. Check the key.");
    expect(localizeError("连上了，发现 14 个工具", "en")).toBe("Connected, 14 tools available");
    expect(localizeError("Error: Failed to fetch", "zh-CN")).toBe("网络请求失败，请检查连接后重试。");
    expect(localizeError("Add a model in Settings first", "zh-CN")).toBe("请先在设置中添加模型");
  });

  it("updates existing dynamic and form errors after switching back to Chinese", () => {
    for (const original of [
      "回答未完成（APIConnectionError），可重试。", "模型输出提前结束（length），可以重试。",
      "MCP「我的工具」连接失败，请在设置中测试连接。", "未找到 MCP 启动命令：/my path/server。请确认已经安装。",
      "连上了，发现 14 个工具", "请求失败（502），请重试。",
      "API 地址需要以 https:// 或 http:// 开头。", "保存失败，理解草稿已保留。",
      "启动参数必须是字符串数组；无参数时填写 []。",
    ]) {
      const english = localizeError(original, "en");
      expect(english).not.toBe(original);
      expect(localizeError(english, "zh-CN")).toBe(original);
    }
  });

  it("preserves diagnostic identifiers and unknown provider details", () => {
    expect(localizeError("回答未完成（APIConnectionError），可重试。", "en")).toContain("APIConnectionError");
    const unknown = "自定义服务：保留原始错误详情 42";
    expect(localizeError(unknown, "en")).toBe(unknown);
    expect(localizeError("我的笔记：模型不存在只是一个例子", "en")).toBe("我的笔记：模型不存在只是一个例子");
  });
});

describe("managed MCP display labels", () => {
  it("localizes only the unchanged defaults of recognized managed servers", () => {
    expect(mcpDisplayName(server, "en")).toBe("Learning files");
    expect(mcpDisplayName(server, "zh-CN")).toBe("学习资料");
    expect(mcpDisplayName({ ...server, label: "我的资料" }, "en")).toBe("我的资料");
    expect(mcpDisplayName({ ...server, args: ["/another/server.py", "filesystem"] }, "en")).toBe("学习资料");
    expect(mcpDisplayName({ ...server, args: [server.args[0], "custom"] }, "en")).toBe("学习资料");
  });
});
