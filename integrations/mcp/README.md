# Optional MCP tools / 可选 MCP 工具

Six pinned local servers extend LearningTree with web search and reading, a learning-file library, persistent knowledge memory, structured thinking, browser actions, and time conversion. They do not require extra API keys by default. A configured chat model with tool support is still needed; the offline demo model does not call external tools.

学习树内置六个可选 MCP 预设。工具本身无需额外 API Key；模型调用仍使用你自己的配置。演示模型用于离线体验，不会执行实际工具。

| Tool / 工具 | Package | Version |
| --- | --- | --- |
| Web search / 联网搜索 | LearningTree's [combined server](web_research.py): DDGS + [mcp-server-fetch](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch) | `ddgs 9.16.0` / `fetch 2026.8.18` |
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

# Upgrade only the old managed Web reader preset, keeping its ID and enabled state.
uv run python integrations/mcp/register.py --preset web-research

# Optional: actual network/tool smoke test; no paid model calls.
uv run python integrations/mcp/verify.py
```

Install is separate from registration. `install.py --register` combines them when the app is already running. `bash integrations/mcp/install.sh` is a compatibility entry point. Installation uses pinned local packages rather than downloading `@latest` on every tool call. Python MCPs use `.runtime/mcp-python/` so their dependencies do not replace the application's SDK.

安装不会自动更改聊天数据库。注册前会备份本机数据库；重复注册保留自定义服务、自定义名称和本组服务原有的启停状态。旧预设「网页阅读」会原位升级为「联网搜索」，保留 ID；也可以用 `--preset web-research` 只升级这一项。移动项目后重新注册即可更新路径。默认注册目标是本机正式数据服务 `8099`，不是开发环境 `8100`。

Open **Settings → MCP tools**, test or enable a server, then select the tools from the chat composer's tool menu. The model decides which selected tools to call.

在「设置 → MCP 工具」管理服务，提问前在输入框的工具菜单中勾选需要的工具。

**Web search** is one MCP server with two tools: `web_search` finds sources, and `read_webpage` extracts their text with pagination. Check it once; the model can search, read a supplied URL, or continue reading a long page as needed. When this managed preset is enabled, the composer hides the two legacy built-in network toggles. Installations without it retain those built-ins. Selecting the preset does not automatically download every search result or grant browser interaction.

「联网搜索」一个勾选项包含搜索和网页正文阅读，模型按需要调用；长文可分段继续读。启用此预设后，输入框不再重复显示内置的「联网搜索」「读取网页」。未安装或停用此预设时，内置工具仍可使用。更新前让正在生成的回答完成，更新后重启应用并刷新页面。

## Data and process boundaries / 数据与会话

- **Learning files:** the project's `learning-materials/` directory is exposed for reading and writing. An existing legacy `学习资料/` directory remains accessible in place; files are never moved, merged or overwritten during this transition. Both libraries are excluded from Git except the public `learning-materials/README.md`. A symlink replacing either library root is rejected. Restart the app after upgrading to refresh cached MCP sessions.
- **Knowledge memory:** `mcp-data/knowledge.jsonl` persists across restarts. It is independent of chat history and is not included in learning-tree JSON exports; back it up separately if needed.
- **Browser:** runs headless with an isolated, temporary profile. On macOS, installed Google Chrome is used when available; otherwise the installer downloads Playwright Chromium. It does not attach to your existing browser or reuse its logins. Output is stored under `mcp-data/browser/`; the current model tool channel passes text and page structure.
- **Time:** uses the operating system's timezone by default; tool calls may specify any supported IANA timezone.
- **Credentials:** the launcher forwards basic OS, locale, proxy and certificate settings, never model API keys from the application environment. Only the combined Web search preset receives the optional `TAVILY_API_KEY` from the backend environment; other MCP servers do not inherit it.
- **Continuity:** the backend reuses each MCP process and session. Navigation and subsequent page queries share a browser session. Closing the app or reconnecting resets browser/thinking state; files and saved memory remain.

文件工具的读写范围为 `learning-materials/`，并兼容原有的 `学习资料/` 目录；升级不会搬动或合并私人文件，两个目录中的资料都继续排除在 Git 之外。升级后重启应用以刷新 MCP 会话。MCP 不是通用系统沙箱。网页阅读保留上游默认的 robots.txt 行为。分步思考工具保存分解步骤，不会改变模型本身的推理能力。

Both the combined MCP and the legacy built-in search use DDGS when no search key is configured. Setting `TAVILY_API_KEY` in the backend process environment opts into Tavily. Failures and empty results are reported explicitly; simulated search results are never substituted. Web reading uses the upstream fetch extractor and retains its default robots.txt checks.

The optional verifier checks all six services using a public example page and uniquely named temporary data. It cleans up its test file and memory entity, closes test sessions, and writes `artifacts/mcp-installation.json`. This tests tool execution, not the capabilities of a particular remote model.

## Suggested additions / 可考虑的扩展

These are recommendations, not installed presets:

- **Programming study:** [Context7](https://context7.com/docs/overview) retrieves library documentation and examples for specific versions. This is the most direct addition when learning frameworks and APIs.
- **Learning from real projects:** [GitHub's official MCP server](https://github.com/github/github-mcp-server) can read repositories, files and issues. Start with its read-only mode and the relevant repository tools; choose authentication and transport before integrating it with LearningTree.
- **Broader research:** [Tavily MCP](https://docs.tavily.com/documentation/mcp) offers search and page extraction. LearningTree's combined preset already supports search and reading, including optional Tavily search, so add the separate server only when its additional tools are needed.

建议按学习任务选工具：先补技术文档，再按需接入开源项目阅读；现有网页、文件、浏览器能力继续使用。内置学习记忆与独立 Memory MCP 是两套存储，删除学习树只清理有明确归属的应用数据。共享 MCP 文件和知识图谱需要独立管理；下一步若要统一清理，应先给 MCP 产物记录所属学习树，再提供按树管理入口。
