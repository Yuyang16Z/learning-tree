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

## Remote servers / 远程 MCP 服务

Besides local programs, **Settings → MCP tools → Add MCP** can connect a remote MCP server over Streamable HTTP: choose **Remote HTTP**, enter its URL (for example `https://example.com/mcp`) and, when the server needs them, request headers such as `Authorization: Bearer <token>`.

- **Credentials:** header values are stored in the local SQLite database like model keys, without encryption at rest. The API and Settings list header names only; when editing, leave a value blank to keep it, or remove the row to delete the header. Put credentials in headers, not in the URL, which Settings shows.
- **Addresses:** the URL must use `http` or `https` without a user name or password. Before connecting, every address of its host must be public: loopback, private, link-local (including cloud metadata) and shared CGNAT (including Tailscale) addresses are refused unless **Allow private addresses** is checked, which is meant for a development server on this computer. Redirects are never followed. The client resolves the name again when it connects, so DNS rebinding after the check is not prevented.
- **Errors:** **Test connection** reports fixed messages (refused address, rejected credentials, missing endpoint, redirect, unreachable, timeout) instead of raw errors, so no header value can appear in them. A failed tool call gets the same fixed messages, except that a call which timed out or lost its connection after it was sent is reported to the model as having an unknown outcome (the server may have run it), never as an unreachable server, so a tool with side effects is checked before it is called again.
- **Sessions and tool lists:** like a local server, each remote server keeps one session. An expired or lost session reconnects once to list tools; a tool call is never repeated. A new connection answered with a redirect or a client error (no MCP endpoint at the URL, rejected credentials) is reported at once without that retry. A session reuses its tool list for 60 seconds; a server that sends `notifications/tools/list_changed` is listed again on the next question, and **Test connection** always lists afresh.
- **Results:** the complete text of each call is stored with the answer (see [data locations](../../docs/operations.md#data-locations)); the model receives at most 24,000 characters per call and can page through the rest with `read_tool_result`.
- **Trust:** a remote server receives the arguments of the calls the model makes to it, which can include parts of your question. Configure only servers you trust.

除本机程序外，「设置 → MCP 工具 → 添加 MCP」还可以选择「远程 HTTP」，通过 Streamable HTTP 连接远程 MCP 服务：填写服务地址（如 `https://example.com/mcp`），需要时添加 `Authorization: Bearer <token>` 之类的请求头。请求头的值与模型密钥一样只保存在本机 SQLite 中（未加密），接口和设置页只显示名称；编辑时留空即保留原值。凭据请放在请求头里，不要写进地址。连接前会检查地址：只接受不含用户名和密码的 http(s) 地址，主机解析出的每个地址都必须是公网地址；本机、内网、链路本地和 Tailscale 等地址会被拒绝，除非勾选「允许内网地址」（仅用于这台电脑上的开发服务）。不会跟随重定向；连接时会再次解析域名，因此无法防止检查之后的 DNS 重绑定。「测试连接」只显示固定提示，不会回显原始错误或请求头；工具调用失败时同样只返回固定提示，但请求发出后超时或连接中断时，模型收到的是「执行结果未确认」（服务可能已经执行），而不是「无法连接」，以免直接重复调用有副作用的工具。已建立的会话过期或中断时，列出工具会重连一次，工具调用不会重复执行；新连接收到重定向或客户端错误（地址上没有 MCP 服务、凭据被拒）时直接报告，不再重试。每个服务的工具清单缓存 60 秒；服务发出 `notifications/tools/list_changed` 后下一次提问会重新获取，「测试连接」总是重新获取。远程服务会收到模型调用它时的参数，其中可能包含问题内容，请只配置可信的服务。

## Approval before tools run / 运行前确认

Each MCP server has **Ask before running tools** in its settings:

- **Ask for tools the server does not mark read-only** (the default, also for servers added before this setting existed): a call waits for you unless the server marks the tool `readOnlyHint`. The bundled presets mark their tools, so web search and reading, file listing and reading, memory lookups and step-by-step thinking run at once, while writing, editing or moving files and changing the knowledge memory ask first. A server that marks nothing asks before every call; choose **Never ask** for one you trust.
- **Always ask**: every call of this server waits for you, whatever the server claims.
- **Never ask**: the model runs this server's tools directly, as before.

A waiting call pauses the answer: the label reads **Waiting for your approval**, and a card shows the tool and the arguments it would run with, with **Allow** and **Don't allow**. The card appears in the window that asked, and comes back when that window reconnects after a dropped connection (a phone that locked, say); a reloaded page or another device only shows the answer as running. A declined call, or one not answered within `TOOL_CONFIRM_TIMEOUT_SECONDS` (default 120, `.env`), does not run: the model is told so and continues without it, and the tool step reads *Not allowed, not run* or *Not confirmed in time, not run*. **Stop** ends the answer without running the call. Time spent waiting is reported as its own phase in the answer diagnostics and never counts against `AGENT_TIME_LIMIT_SECONDS`. The read-only mark comes from the server itself: a server that mislabels a tool bypasses the default, so use **Always ask** for servers you do not fully trust. The app's own readers and the built-in web tools only read and never ask.

每个 MCP 服务的设置里都有「运行工具前确认」：默认「服务未标为只读的工具先问我」（升级前已添加的服务也是这个默认值）——服务没有用 `readOnlyHint` 标为只读的工具要等你确认才运行。内置预设都标注了自己的工具，所以联网搜索和读取网页、列出和读取文件、查询知识记忆、分步思考直接运行，写入、编辑、移动文件和修改知识记忆会先问你；完全不做标注的服务每次调用都会问，信任它时可改为「直接运行，不问我」。「每次都先问我」不管服务如何声明都会询问。需要确认时回答暂停，状态显示「等待你确认」，卡片列出工具和将要使用的参数，提供「允许运行」和「不允许」。卡片出现在提问的窗口里，连接中断后重新连上（例如手机锁屏后解锁）会再次出现；刷新后的页面或其他设备只显示回答正在进行。不允许或在 `TOOL_CONFIRM_TIMEOUT_SECONDS`（默认 120 秒）内没有回应时，这次调用不执行，模型会被告知并继续回答，工具步骤显示「未允许，没有运行」或「确认超时，没有运行」；按「停止」会结束回答且不执行该调用。等待时间在回答诊断里单独列出，不计入 `AGENT_TIME_LIMIT_SECONDS`。只读标记由服务自己提供，标错的服务可以绕过默认设置，不完全信任的服务请选「每次都先问我」。应用自带的回读工具和内置联网工具只读取，不会询问。

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
- **Learning from real projects:** [GitHub's official MCP server](https://github.com/github/github-mcp-server) can read repositories, files and issues. It can be added as a remote HTTP server with an `Authorization` header; start with its read-only mode and the relevant repository tools.
- **Broader research:** [Tavily MCP](https://docs.tavily.com/documentation/mcp) offers search and page extraction. LearningTree's combined preset already supports search and reading, including optional Tavily search, so add the separate server only when its additional tools are needed.

建议按学习任务选工具：先补技术文档，再按需接入开源项目阅读；现有网页、文件、浏览器能力继续使用。内置学习记忆与独立 Memory MCP 是两套存储，删除学习树只清理有明确归属的应用数据。共享 MCP 文件和知识图谱需要独立管理；下一步若要统一清理，应先给 MCP 产物记录所属学习树，再提供按树管理入口。
