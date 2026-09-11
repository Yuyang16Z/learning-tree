# Optional MCP tools / 可选 MCP 工具

Six pinned local servers extend LearningTree with webpage reading, a learning-file library, persistent knowledge memory, structured thinking, browser actions, and time conversion. They do not require extra API keys. A configured chat model with tool support is still needed; the offline demo model does not call external tools.

学习树内置六个可选 MCP 预设。工具本身无需额外 API Key；模型调用仍使用你自己的配置。演示模型用于离线体验，不会执行实际工具。

| Tool / 工具 | Package | Version |
| --- | --- | --- |
| Web reading / 网页阅读 | [mcp-server-fetch](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch) | `2026.8.18` |
| Learning files / 学习资料 | [server-filesystem](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem) | `2026.8.31` |
| Knowledge memory / 知识记忆 | [server-memory](https://github.com/modelcontextprotocol/servers/tree/main/src/memory) | `2026.8.31` |
| Sequential thinking / 分步思考 | [server-sequential-thinking](https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking) | `2026.8.31` |
| Browser / 浏览器 | [Playwright MCP](https://github.com/microsoft/playwright-mcp) | `0.0.80` |
| Time / 时间查询 | [mcp-server-time](https://github.com/modelcontextprotocol/servers/tree/main/src/time) | `2026.8.18` |

## Install / 安装

Run the main project's setup first. MCP dependencies are optional and are not downloaded during normal setup or CI tests.

先执行项目的 `manage.py setup`。MCP 安装独立于常规项目安装与测试。

```sh
# Install from package-lock.json and requirements.lock, without changing the database.
uv run python integrations/mcp/install.py

# Linux only: also install missing browser system libraries if needed.
# Playwright may request sudo for the OS package manager.
uv run python integrations/mcp/install.py --with-browser-deps

# Start the normal local app in a separate terminal.
uv run python scripts/manage.py start

# Register the six presets in the running localhost:8099 app.
uv run python integrations/mcp/register.py

# Optional: actual network/tool smoke test; no paid model calls.
uv run python integrations/mcp/verify.py
```

Install is separate from registration. `install.py --register` combines them when the app is already running. `bash integrations/mcp/install.sh` is a compatibility entry point. Installation uses pinned local packages rather than downloading `@latest` on every tool call. Python MCPs use `.runtime/mcp-python/` so their dependencies do not replace the application's SDK.

安装不会自动更改聊天数据库。注册前会备份本机数据库；重复注册保留自定义服务和本组服务原有的启停状态。移动项目后重新注册即可更新路径。默认注册目标是本机正式数据服务 `8099`，不是开发环境 `8100`。

Open **Settings → MCP tools**, test or enable a server, then select the tools from the chat composer's tool menu. The model decides which selected tools to call.

在「设置 → MCP 工具」管理服务，提问前在输入框的工具菜单中勾选需要的工具。

## Data and process boundaries / 数据与会话

- **Learning files:** only the project's `学习资料/` directory is exposed for reading and writing. Place your own files there; they are excluded from Git. A symlink replacing the entire library directory is rejected.
- **Knowledge memory:** `mcp-data/knowledge.jsonl` persists across restarts. It is independent of chat history and is not included in learning-tree JSON exports; back it up separately if needed.
- **Browser:** runs headless with an isolated, temporary profile. On macOS, installed Google Chrome is used when available; otherwise the installer downloads Playwright Chromium. It does not attach to your existing browser or reuse its logins. Output is stored under `mcp-data/browser/`; the current model tool channel passes text and page structure.
- **Time:** uses the operating system's timezone by default; tool calls may specify any supported IANA timezone.
- **Credentials:** the launcher forwards only basic OS, locale, proxy and certificate settings, never model API keys from the application environment.
- **Continuity:** the backend reuses each MCP process and session. Navigation and subsequent page queries share a browser session. Closing the app or reconnecting resets browser/thinking state; files and saved memory remain.

文件工具的读写范围为 `学习资料/`；MCP 不是通用系统沙箱。网页阅读保留上游默认的 robots.txt 行为。分步思考工具保存分解步骤，不会改变模型本身的推理能力。

The separate built-in **Web search** tool uses DDGS when no search key is configured. `TAVILY_API_KEY` opts into Tavily. Failures and empty results are reported explicitly; simulated search results are never substituted.

The optional verifier checks all six services using a public example page and uniquely named temporary data. It cleans up its test file and memory entity, closes test sessions, and writes `artifacts/mcp-installation.json`. This tests tool execution, not the capabilities of a particular remote model.
