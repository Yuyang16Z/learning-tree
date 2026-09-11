# LearningTree · 学习树

[English](README.md) · [MIT 许可](LICENSE) · [参与开发](CONTRIBUTING.md)

一个给自己用的 AI 学习空间：围绕主问题连续聊天，不懂的概念展开分支，弄懂后回到原句继续。

## 能做什么

- 连续问答与话题树并排展示，点击节点跳到对应轮次。地图支持拖动、缩放、定位，并能恢复定位前的视角。
- 选中文字「解释一下」，或者从回答创建「新分支」，结束后「返回出处」定位原文。
- 分支标题概括你在分支里提出的第一个问题，不再截取上一条回答；尚未提问时显示“新分支”。
- 保存「我的理解」，用「带着理解继续」把理解放入主线草稿，编辑后再发送。
- 编辑问题生成新版本，原问答保留；回答中断后可以重试。
- 在 **设置 → 语言 / Language** 即时切换 **中文 / English**，保留草稿、聊天内容和笔记原文。
- 支持 OpenAI 兼容 Chat Completions 和 Anthropic 原生 Messages；可选 MCP 提供网页阅读、学习资料、知识记忆、浏览器等工具。
- 导出、导入整棵学习树，导入不会覆盖原记录。

这是一个**本机单用户应用**。离线演示使用固定回复展示交互，不是真实 AI 模型。

## 快速开始

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 [Node.js 24 LTS](https://nodejs.org/en/download)。Python 要求 3.11+，uv 可自动安装 `.python-version` 指定版本；Node.js 最低支持 22.12。

```sh
git clone https://github.com/Yuyang16Z/learning-tree.git
cd learning-tree
uv run python scripts/manage.py setup
uv run python scripts/manage.py start --open
```

打开 **http://127.0.0.1:8099**，保留终端运行，Ctrl+C 停止。macOS 安装后也可以双击 `启动学习树.command`。

在「设置 → 模型与 API key」添加地址、模型 ID 和 API Key。想先离线体验，可运行带演示模型的独立开发环境：

```sh
uv run python scripts/manage.py dev
```

打开 **http://127.0.0.1:5174**。也可以导入 [`examples/learning-tree.sample.json`](examples/learning-tree.sample.json) 查看示例，不需要密钥。演示回复目前使用中文。

## 配置

正常使用不必创建 `.env`，直接在设置里管理模型。如需更换数据库位置或初始化默认模型，将 `.env.example` 复制成 `.env` 后在本机编辑。

| 配置项 | 说明 |
| --- | --- |
| OpenAI 兼容 | 使用 Chat Completions，地址和模型 ID 按服务商说明填写。 |
| Anthropic | 原生 Messages，支持流式输出、图片和工具。官方地址 `https://api.anthropic.com`，最大输出默认 4,096 tokens。 |
| 编辑模型密钥 | 留空保留原密钥。 |
| MCP 工具 | 按 [MCP 说明](integrations/mcp/README.md) 安装、注册六个可选预设，或自行添加服务。 |
| 联网搜索 | 默认 DDGS，配置 `TAVILY_API_KEY` 时使用 Tavily，失败明确提示。 |

能力取决于供应商与模型。尚未支持原生 Responses、Gemini 协议。切换界面语言不自动翻译历史内容；划词解释跟随所选语言，常规回复由问题和模型决定。

新分支的首个问题会使用当前模型额外发起一次简短的标题请求，后台完成，不阻塞回答。只传入问题与少量来源摘录，不调用工具或发送图片；超时、失败或结果无效时保留问题文本。自定义、导入的标题保持原样，后续追问不会反复改名。

## 开发与验证

```sh
uv run python scripts/manage.py dev      # 后端8100、前端5174，使用.runtime/dev.db
uv run python scripts/manage.py check    # Python规范、测试、TypeScript检查与构建

# 首次浏览器检查先安装测试浏览器
node web/node_modules/playwright/cli.js install chromium --only-shell
uv run python scripts/manage.py e2e
```

开发环境使用独立数据库，首次初始化离线演示模型；之后自行添加的真实模型会保留，也可能发起实际请求。测试固定使用模拟模型；浏览器检查使用临时数据库和随机本机端口，不依赖个人聊天库或付费模型。GitHub Actions 在推送和合并请求时自动运行相同检查。

```text
app/                 后端路由、模型适配、上下文、数据库和MCP运行时
web/src/             聊天、树图、设置及国际化界面
tests/               后端回归测试
scripts/             安装、启动、备份和验证命令
integrations/mcp/    可选MCP依赖与启动器
examples/            虚构示例数据和最小MCP服务
docs/                架构和本机运行说明
```

详见 [架构说明](docs/architecture.md)、[运行与备份](docs/operations.md) 和 [贡献指南](CONTRIBUTING.md)。

## 数据与使用范围

默认数据文件为 `branch_learning.db`，保留旧文件名和 JSON 格式标识以兼容旧记录。启动时先备份已有数据库到 `.backups/`，再构建界面；同一个数据库只运行一个后端进程。

聊天、笔记、模型密钥和工具配置在本机保存。密钥保存在私人 SQLite 文件中，**没有静态加密**。草稿和阅读位置在浏览器中。学习树导出不含模型密钥或服务配置，但包含聊天、附件；MCP 记忆和学习资料需要单独备份。

当前没有账户鉴权，MCP 能运行本机程序。启动命令只监听 `127.0.0.1`，适用于个人本机使用；公网、多用户部署需要另行增加访问控制，不属于本版支持范围。参见 [安全说明](SECURITY.md)。

数据库、密钥、`.env`、个人资料、备份和生成产物均排除在 Git 之外；仓库只提供虚构示例。

## 开源许可

[MIT](LICENSE) © 2026 Yuyang16Z。第三方工具遵循各自许可。
