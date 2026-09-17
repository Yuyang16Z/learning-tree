# LearningTree · 学习树

[English](README.md) · [MIT 许可](LICENSE) · [参与开发](CONTRIBUTING.md)

一个给自己用的 AI 学习空间：围绕主问题连续聊天，不懂的概念展开分支，弄懂后回到原句继续。

![LearningTree 主界面：连续对话与学习分支](docs/assets/learning-tree-overview.png)

<details>
<summary>查看演示：划词解释 → 分支追问 → 带着理解返回</summary>

![离线演示：展开分支并返回原文](docs/assets/learning-tree-demo.gif)

</details>

图片使用虚构内容与离线演示模型，展示产品交互，不代表 AI 回答质量；真实回答需要自行配置模型。

## 能做什么

- 连续问答与话题树并排展示，点击节点跳到对应轮次。地图支持拖动、缩放、定位，并能恢复定位前的视角。
- 对话旁的问题导航条支持悬停预览、点击跳到历史提问；定位只改变阅读位置，保留当前分支和草稿。
- 侧栏主题的 **…** 菜单支持改名、归档和删除。归档保留聊天与分支，在「设置 → 已归档」中可查看、改名、恢复或删除旧主题；改名不会修改最初的问题。
- 确认删除会移除整棵树的对话、已上传附件及关联记忆，并清理本机草稿；应用内不提供撤销。[独立备份和共享资料的管理范围](docs/operations.md#troubleshooting)另行说明。
- 选中文字可以复制、「解释一下」或创建「新分支」，结束后「返回出处」定位原文。
- 分支标题概括你在分支里提出的第一个问题，不再截取上一条回答；尚未提问时显示“新分支”。
- 保存「我的理解」，用「带着理解继续」把理解放入主线草稿，编辑后再发送。
- 编辑问题生成新版本，原问答保留；回答中断后可以重试。
- 问题和回答旁有简约的一键复制按钮；回形针附件入口统一接收图片和文档，也支持混选。
- 给问题附上 PDF、Word `.docx` 或文本资料，可预览解析文本、下载原文件；后续追问、重试与编辑保留文档来源。详见[格式限制及文档如何进入模型上下文](docs/documents.md#在对话中使用文档)。
- 按模型容量管理长对话：保留近期完整问答，从较早内容抽取带来源的完整原句，需要时可由 Agent 回读当前路径原文。
- 在 **设置 → 语言 / Language** 即时切换 **中文 / English**，保留草稿、聊天内容和笔记原文。
- 支持 OpenAI 兼容 Chat Completions 和 Anthropic 原生 Messages；可选 MCP 提供网页阅读、学习资料、知识记忆、浏览器等工具。
- 导出、导入整棵学习树，导入不会覆盖原记录。
- 按当前问题检索相关记忆：先限定当前学习路径，再结合关键词与本地多语言向量检索，重排候选并保留来源；用户偏好独立使用。设置中可查看检索状态、准备模型和删除记忆。
- 在一个文本框中编辑偏好与习惯；话题记忆支持搜索、按主题筛选、分页、修改、批量删除和返回来源。

这是一个**本机单用户应用**。离线演示使用固定回复展示交互，不是真实 AI 模型。

## 快速开始

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 [Node.js 24 LTS](https://nodejs.org/en/download)。Python 要求 3.11+，uv 可自动安装 `.python-version` 指定版本；Node.js 最低支持 22.12。

```sh
git clone https://github.com/Yuyang16Z/learning-tree.git
cd learning-tree
uv run python scripts/manage.py setup
uv run python scripts/manage.py dev
```

打开 **http://127.0.0.1:5174**，无需 API Key 即可体验离线演示。也可以导入 [`examples/learning-tree.sample.json`](examples/learning-tree.sample.json) 查看示例。演示回复目前使用中文。保留终端运行，Ctrl+C 停止。

基础安装不安装 PyTorch、Transformers，也不下载本地模型权重；记忆使用关键词检索，语义检索可以之后按需添加。

开始自己的学习时，先停止演示，再运行：

```sh
uv run python scripts/manage.py start --open
```

打开 **http://127.0.0.1:8099**，在「设置 → 模型与 API key」添加地址、模型 ID 和 API Key。正式使用与演示使用不同数据库。macOS 安装后也可以双击 `start-learning-tree.command`。

### macOS 桌面应用

完成项目安装后，可安装到 Mac 的「应用程序」（macOS 14+）：

```sh
uv run --inexact python desktop/macos/build.py --install
```

在「应用程序」里打开 **LearningTree / 学习树** 即可使用，不需要另开终端，也不默认创建桌面快捷方式。应用会复用已经运行的服务；服务未运行时，先备份记录再自动启动。聊天记录、模型配置和文档沿用原项目，桌面版的草稿和界面偏好与浏览器分别保存。请保留原项目文件夹；这是本机桌面版，还不是可直接分发到其他电脑的独立安装包。详见[桌面版安装与使用](desktop/macos/README.md)。

### 可选：语义记忆检索

```sh
uv run python scripts/manage.py retrieval
```

此命令安装 `retrieval` 可选依赖，并准备固定版本的 E5-small 向量模型与 BGE-reranker-v2-m3 重排模型。权重约 **2.55 GiB**，另需分词器、Python 依赖和运行内存，不需要额外 API Key。完成后重启应用，在「设置 → 记忆」查看状态；准备失败时，基础聊天与关键词检索仍可使用。

全新安装也可用 `setup --with-retrieval` 一并准备。若 `.env` 显式设置了 `MEMORY_RETRIEVAL_MODE=lexical`，需改为 `hybrid` 才会启用语义检索。常规启动只后台加载缓存，不自动下载权重。详见[模型准备与故障处理](docs/operations.md#local-memory-retrieval)。

## 配置

正常使用不必创建 `.env`，直接在设置里管理模型。如需更换数据库位置或初始化默认模型，将 `.env.example` 复制成 `.env` 后在本机编辑。

| 配置项 | 说明 |
| --- | --- |
| OpenAI 兼容 | 使用 Chat Completions，地址和模型 ID 按服务商说明填写。 |
| Anthropic | 原生 Messages，支持流式输出、图片和工具。官方地址 `https://api.anthropic.com`，最大输出默认 4,096 tokens。 |
| 编辑模型密钥 | 留空保留原密钥。 |
| 上下文预算 | 在模型「高级设置」填写实际上下文窗口和回答预留；预算估算与原句压缩不需要额外模型或下载。 |
| MCP 工具 | 按 [MCP 说明](integrations/mcp/README.md) 安装、注册六个可选预设，或自行添加服务。 |
| 联网搜索 | 默认 DDGS，配置 `TAVILY_API_KEY` 时使用 Tavily，失败明确提示。 |
| 记忆检索 | 基础安装使用关键词检索；可选本地向量与重排增强语义检索。`MEMORY_RETRIEVAL_MODE=lexical` 可停用语义模型。 |

能力取决于供应商与模型。尚未支持原生 Responses、Gemini 协议。切换界面语言不自动翻译历史内容；划词解释跟随所选语言，常规回复由问题和模型决定。

新分支的首个问题会额外发起一次简短的后台标题请求，只传入问题与来源摘录，不调用工具或发送图片；失败时保留问题文本。已有、自定义和导入的标题不会因普通追问改名。

## 开发与验证

```sh
uv run python scripts/manage.py dev      # 后端8100、前端5174，使用.runtime/dev.db
uv run python scripts/manage.py check    # Python规范、测试、TypeScript检查与构建

# 首次浏览器检查先安装测试浏览器
node web/node_modules/playwright/cli.js install chromium --only-shell
uv run python scripts/manage.py e2e
```

开发环境使用独立数据库，首次初始化离线演示模型；之后自行添加的真实模型会保留，也可能发起实际请求。测试固定使用模拟模型；浏览器检查使用临时数据库和随机本机端口，不依赖个人聊天库或付费模型。GitHub Actions 在推送和合并请求时自动运行相同检查。

`dev`、`check`、`e2e` 的隔离环境固定使用关键词检索，不下载或加载检索模型。安装语义检索后，可运行 `uv run python scripts/prepare_retrieval.py --smoke` 验证真实本地推理；它只使用固定的虚构中英文样例，不读取个人聊天数据库。[合成模型核查记录](docs/memory-retrieval-validation.md)保留了固定版本、实测开销与相关性反例。

## 记忆如何参与回答

在「设置 → 记忆」里，已有偏好会先合并显示，打开设置不会修改原记录。首次保存后，这份偏好由你管理，AI 不再自动追加或改写；保存空白内容也会保持为空。临时学习内容归入话题记忆。修改话题事实会保留来源并更新检索缓存；管理界面的跨主题搜索不会扩大聊天可引用的学习路径。详见[记忆管理](docs/memory-management.md)。

完成的非演示问答会在后台提炼用户偏好和主题事实。偏好独立选择；主题事实先按当前学习路径限定来源。可选语义检索结合 BM25 与 E5，通过 RRF 合并候选并用 BGE 重排，保留来源与完整条目。近期对话仍然进入上下文。

SQLite 中原有记忆及节点 ID 保持不变；`MemoryEmbedding` 保存可重建的向量缓存，`PreferenceProfile` 保存用户编辑的偏好。删除记忆、来源分支或整棵树会同步删除对应向量；每次检索仍以原始记录和来源范围为准。删除记忆不会删除原始聊天记录，也不会清除独立 MCP 知识图谱。

此次检索对象是内置记忆；MCP 知识记忆和 `learning-materials/`（包括升级前的 `学习资料/`）尚未接入此索引。语义模型在本地计算，选中的内容仍会随本轮上下文发给你配置的回答模型。相关性分数不代表内容正确，自动提炼的记忆可能有误或过时。

长对话另有逐请求预算检查，工具返回也计入估算。较早对话按类别抽取带来源的完整原句，原始聊天保持不变；估算不是服务商的精确 token 计数，压缩也可能遗漏相关内容。详见[上下文预算、原文回读与限制](docs/context-budget.md)。

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

版本改动与升级步骤：[v0.2.0](docs/releases/v0.2.0.md) · [更新日志](CHANGELOG.md)。

## 数据与使用范围

默认数据文件为 `branch_learning.db`，保留旧文件名和 JSON 格式标识以兼容旧记录。启动时先备份已有数据库到 `.backups/`，再构建界面；同一个数据库只运行一个后端进程。

聊天、笔记、文档原文件和解析文本、模型密钥和工具配置在本机保存。密钥保存在私人 SQLite 文件中，**没有静态加密**。草稿和阅读位置在浏览器中。学习树导出不含模型密钥或服务配置，但包含聊天和附件原文件；MCP 记忆和学习资料需要单独备份。上传文档在本机完成；相关摘录进入问题上下文时，会发给你配置的回答模型。

当前没有账户鉴权，MCP 能运行本机程序。启动命令只监听 `127.0.0.1`，适用于个人本机使用；公网、多用户部署需要另行增加访问控制，不属于本版支持范围。参见 [安全说明](SECURITY.md)。

数据库、密钥、`.env`、个人资料、备份和生成产物均排除在 Git 之外；仓库只提供虚构示例。

## 开源许可

[MIT](LICENSE) © 2026 Yuyang16Z。第三方工具遵循各自许可。
