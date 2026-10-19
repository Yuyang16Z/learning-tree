import { describe, expect, it } from "vitest";
import { localizeError, mcpDisplayName } from "./workspace";
import type { McpServer } from "../types";

const launcher = "/work/My Project/integrations/mcp/launch.py";
const server: McpServer = { id: 1, label: "学习资料", command: "/usr/bin/python3", args: [launcher, "filesystem"], enabled: true };

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
      "模型接口拒绝了本次请求中的工具（可能不支持工具调用）。请换用支持工具的模型，或取消选择工具后重试。",
      "MCP「我的工具」连接失败，请在设置中测试连接。", "未找到 MCP 启动命令：/my path/server。请确认已经安装。",
      "连上了，发现 14 个工具", "请求失败（502），请重试。",
      "API 地址需要以 https:// 或 http:// 开头。", "保存失败，理解草稿已保留。",
      "理解标记没有保存，已恢复原来的标记。", "这个节点不在分支中",
      "启动参数必须是字符串数组；无参数时填写 []。",
      "PDF 没有可提取的文字，可能是扫描件；暂不支持 OCR，请上传文字版。",
      "第 1, 3 页没有可提取的文字（可能为空白页或扫描图片）；未进行 OCR。",
      "文档不存在或不属于当前学习主题，请重新上传。",
      "服务商未提供该模型的上下文长度；以下为内置目录的近似值，请核对后保存。",
      "演示模型没有可检测的上下文窗口。",
      "地址或协议已更改，已保存的 API Key 不会发往新的服务端；请填写它的 API Key 后再检测。以下为内置目录的近似值，请核对后保存。",
      "地址或协议已更改，已保存的 API Key 不会发往新的服务端；请填写它的 API Key 后再检测，或按服务商文档填写。",
      "检索范围设置已在其他窗口更新，请重新载入。",
      "MCP 远程服务拒绝访问，请检查请求头中的凭据。", "新的请求头需要填写值。",
      "MCP 远程地址指向本机或内网；如果是本机开发服务，请在配置中允许内网地址。",
      "工具结果不存在或已删除。", "需要重新配对：请在电脑上打开「设置 → 手机访问」，用手机扫描二维码。",
      "配对链接无效或已更换，请在电脑上重新扫描二维码。", "无法保存手机访问令牌，请检查令牌文件所在目录的权限。",
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
  it("localizes either web-search default while preserving custom and unmanaged labels", () => {
    const search = { ...server, args: [launcher, "web-research"] };
    for (const label of ["联网搜索", "Web search"]) {
      expect(mcpDisplayName({ ...search, label }, "zh-CN")).toBe("联网搜索");
      expect(mcpDisplayName({ ...search, label }, "en")).toBe("Web search");
    }
    expect(mcpDisplayName({ ...search, label: "我的研究助手" }, "en")).toBe("我的研究助手");
    expect(mcpDisplayName({ ...search, label: "My research" }, "zh-CN")).toBe("My research");
    expect(mcpDisplayName({ ...search, label: "Web search", args: ["/custom/server.py", "web-research"] }, "zh-CN")).toBe("Web search");
    expect(mcpDisplayName({ ...search, label: "联网搜索", transport: "http", url: "https://docs.example/mcp" }, "en")).toBe("联网搜索");
  });

  it("localizes only the unchanged defaults of recognized managed servers", () => {
    expect(mcpDisplayName(server, "en")).toBe("Learning files");
    expect(mcpDisplayName(server, "zh-CN")).toBe("学习资料");
    expect(mcpDisplayName({ ...server, label: "我的资料" }, "en")).toBe("我的资料");
    expect(mcpDisplayName({ ...server, args: ["/another/server.py", "filesystem"] }, "en")).toBe("学习资料");
    expect(mcpDisplayName({ ...server, args: [launcher, "custom"] }, "en")).toBe("学习资料");
  });
});
