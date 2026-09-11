# LearningTree

[中文说明](README.zh-CN.md) · [MIT license](LICENSE) · [Contributing](CONTRIBUTING.md)

A personal workspace for learning with AI. Ask a question, explore unfamiliar ideas in branches, and return to the original explanation without losing your place.

## Features

- A continuous conversation alongside an interactive topic map: jump to any turn, pan, zoom and return to your previous view after centering.
- Select text for a quick explanation or create a branch from an answer. Return to the exact source passage when you finish.
- Branch titles summarize the first question you ask, rather than copying the source answer. A new empty branch shows “New branch”.
- Save your understanding and bring it into the main conversation as an editable draft.
- Edit a question as a new version, retry interrupted answers, and keep the original record.
- Switch **English / 中文** in **Settings → 语言 / Language**. Labels change immediately; your conversations and notes keep their original language.
- Connect OpenAI-compatible Chat Completions or native Anthropic Messages. Optional MCP tools add web reading, local files, memory, browser actions, thinking steps and time lookup.
- Export individual trees as JSON and import them without overwriting existing records.

LearningTree is a **local, single-user application**. Its offline demo uses fixed responses to demonstrate interactions; it is not a real AI model.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Node.js 24 LTS](https://nodejs.org/en/download). Python 3.11+ is required; uv can install the version in `.python-version` automatically. Node.js 22.12+ is supported.

```sh
git clone https://github.com/Yuyang16Z/learning-tree.git
cd learning-tree
uv run python scripts/manage.py setup
uv run python scripts/manage.py start --open
```

Open **http://127.0.0.1:8099**. Keep the terminal open; Ctrl+C stops the app. On macOS, after setup, you can also double-click `启动学习树.command`.

In **Settings → Models & API keys**, add your endpoint, model ID and API key. For an offline trial with a preconfigured demo model and separate database:

```sh
uv run python scripts/manage.py dev
```

Open **http://127.0.0.1:5174**. You can also import [`examples/learning-tree.sample.json`](examples/learning-tree.sample.json) to explore a conversation without an API key. The demo's example responses are currently in Chinese.

## Configuration

No `.env` is needed for normal use. Configure models in Settings. To select another data file or seed an initial model, copy `.env.example` to `.env` and edit it locally.

| Option | Behavior |
| --- | --- |
| OpenAI-compatible | Uses Chat Completions; use the base URL and model ID provided by your service. |
| Anthropic | Native Messages, streaming, images and tool calls. Official base URL: `https://api.anthropic.com`. Output limit defaults to 4,096 tokens. |
| Editing an API key | Leaving it empty retains the existing key. |
| Optional MCP | Follow the [MCP guide](integrations/mcp/README.md) to install and register six presets, or configure your own server. |
| Web search | DDGS by default; `TAVILY_API_KEY` opts into Tavily. Errors are reported explicitly. |

Capabilities depend on the provider and model. Native OpenAI Responses and Gemini protocols are not implemented. UI language does not translate saved content; quick explanations follow the selected language, and normal answers depend on the prompt and model.

Each new branch's first question uses one additional short title request to the selected model, in the background, without tools or images. Only the question and a short source excerpt are sent. This does not delay the answer; unavailable, slow or invalid results keep a question-based label. Custom/imported titles are preserved, and ordinary follow-ups do not rename an established branch.

## Development and checks

```sh
uv run python scripts/manage.py dev      # API :8100, Vite :5174, .runtime/dev.db
uv run python scripts/manage.py check    # Python lint/format, tests, TypeScript, build

# Install the test browser once.
node web/node_modules/playwright/cli.js install chromium --only-shell
uv run python scripts/manage.py e2e
```

Development uses a separate SQLite file and seeds an offline demo on first use. Models you later add to that development database remain available and may make real requests. Tests use fixed mock providers; browser tests use a temporary database and random loopback port without personal data or paid calls. GitHub Actions runs these checks on pushes and pull requests.

```text
app/                 FastAPI routes, providers, context, persistence and MCP runtime
web/src/             React workspace, map, chat, settings and localization
tests/               Backend regression tests
scripts/             Portable setup, development, backup and verification commands
integrations/mcp/    Optional pinned MCP tools and launchers
examples/            Synthetic sample data and a minimal MCP server
docs/                Architecture and local operation guide
```

See [architecture](docs/architecture.md), [operation and backups](docs/operations.md), and [contribution guidelines](CONTRIBUTING.md).

## Your data

The default data file is `branch_learning.db`; the legacy filename and JSON format identifier are retained for compatibility. Startup backs up an existing database into `.backups/` before building the UI. Run only one backend per database.

Chats, notes, model keys and tool configuration are stored locally. Keys are in the private SQLite file, **not encrypted at rest**. Drafts and reading positions stay in browser storage. Tree exports omit model keys and server configuration but contain the exported conversation and attachments. MCP memory and learning files require separate backups.

The API has no account authentication; configured MCP servers can execute local programs. Supplied commands bind to `127.0.0.1`. Internet or shared-server deployment requires additional access controls and is outside this release's scope. See [security policy](SECURITY.md).

Databases, keys, `.env`, private learning files, backups and generated artifacts are excluded from Git. Only synthetic examples are included in this repository.

## License

[MIT](LICENSE) © 2026 Yuyang16Z. Optional third-party tools retain their own licenses.
