# Local operation

Run commands from the repository root with uv and Node.js 24 installed.

| Command | Purpose |
| --- | --- |
| `uv run python scripts/manage.py setup` | Install dependencies from lockfiles and prepare pinned local retrieval models. |
| `uv run python scripts/manage.py start --open` | Back up data, build and start on `127.0.0.1:8099`; open an existing app if already running. |
| `uv run python scripts/manage.py dev` | Isolated development on 8100/5174 with `.runtime/dev.db`; initially seeds a demo model, retains later user configurations, and uses lexical retrieval. |
| `uv run python scripts/manage.py backup` | Online SQLite backup to `.backups/`. |
| `uv run python scripts/manage.py check` | Lint, format, unit/regression tests and frontend build; temporary test database and lexical retrieval. |
| `uv run python scripts/manage.py e2e` | UI checks with a temporary database, random local port and lexical retrieval. |
| `uv run python scripts/prepare_retrieval.py --smoke` | Prepare/download local retrieval models and verify real inference on synthetic Chinese/English examples. |

An occupied port is not forcibly freed. Ctrl+C stops the app. Use one backend per database; the standard launcher guards its port, but cannot police custom commands on other ports.

## Data locations

| Location | Contains |
| --- | --- |
| `branch_learning.db` | Normal chats, model keys, notes, memory, rebuildable `MemoryEmbedding` vectors and tool configuration. |
| `.runtime/dev.db` | Separate development data and demo configuration. |
| `.runtime/retrieval/models/` | Pinned public embedding/reranker weights; no chat records. |
| `.backups/` | Private database copies including credentials. |
| `学习资料/` | Files exposed to the optional file tool. |
| `mcp-data/` | MCP knowledge graph and browser artifacts. |
| Browser storage | Drafts, positions, delete-recovery snapshot and language. |

These local data files are excluded from Git. `DATABASE_URL` may select another SQLite file. Supplied commands resolve relative paths from the project root.

## Local memory retrieval

Hybrid retrieval is enabled by default and needs no additional API key. Initial setup downloads multilingual E5-small and BGE-reranker-v2-m3, approximately 2.55 GiB of public weights in addition to tokenizers and runtime dependencies. Both run on CPU with two PyTorch compute threads; loaded models also consume memory, so weight-file size is not a RAM limit. See [measured local costs and relevance limitations](memory-retrieval-validation.md). Normal startup only warms cached models in the background; neither startup nor answering a question downloads missing weights.

Open **Settings → Memory** to inspect readiness. While local models are being prepared, or if embeddings are unavailable, answers can continue using keyword retrieval. If embeddings are ready but the reranker is unavailable, hybrid retrieval still works without the second-stage rerank. The panel reports these states rather than silently claiming semantic search is ready. **Prepare / retry** explicitly starts a background download/load attempt; it does not send chat content to a retrieval service. Selected memories are still included in prompts sent to the configured answer model.

The management API uses `GET /api/memories/retrieval/status` for read-only status and `POST /api/memories/retrieval/prepare` to prepare models. Both return `state`, `embedding_ready` and `reranker_ready`; the status route itself never downloads models. The settings panel polls briefly during preparation, then offers manual refresh if the background work takes longer.

For keyword-only operation, set `MEMORY_RETRIEVAL_MODE=lexical` in `.env` and restart. Setting this before setup skips retrieval-model preparation. Development, unit/regression checks and browser tests force lexical mode in their isolated environment and do not download weights. For real local model validation, use `prepare_retrieval.py --smoke` with hybrid mode enabled. This command uses synthetic examples and never opens the chat database; its successful result does not establish real-answer quality or factual correctness.

Existing memories do not need to be imported again. Their IDs and content are unchanged, and missing embedding-cache entries are generated lazily during retrieval. The cache is derived data: a restored database without these vectors can rebuild them from its memory records. A missing model-weight cache is restored by preparation. If you prepare weights with the CLI while the app is already running, use **Prepare / retry** in that process or restart to load them.

Deleting one memory, clearing memory, or deleting a source branch/tree also removes associated cached vectors. Deleting an extracted memory does not delete the original chat messages, which may still enter the current conversation's context. These controls do not clear MCP memory or index/delete files in `学习资料/`; those are independent stores.

## Updating and restoring

1. Let active answers finish and stop the app.
2. Run `uv run python scripts/manage.py backup`.
3. Pull the update; run `uv run python scripts/manage.py setup`.
4. Start again and refresh the browser. Additive upgrades preserve existing IDs and records.

For full recovery, stop the app, keep a copy of the current data file, and copy a chosen private backup to the configured database path. Start one backend. A database restore does not restore browser drafts, learning files or MCP memory; back those up separately when moving machines.

Tree JSON import creates a new tree and remaps references. Limits are 25 MB, 2,000 nodes and 12,000 messages. Automatic preference/factual memory and MCP memory are not included. Exported conversations can still be private even though model keys are excluded.

## Troubleshooting

- **No model:** add one in Settings, or run `manage.py dev` for the offline demo.
- **Old UI:** refresh after rebuilding/restarting. Clearing browser storage also removes drafts and preferences.
- **Semantic retrieval is unavailable:** check Settings → Memory and use **Prepare / retry**, or run `uv run python scripts/prepare_retrieval.py --smoke`. Initial preparation needs access to public Hugging Face model files. Keyword retrieval remains available if preparation fails; no new model API key is required.
- **MCP path errors:** install dependencies and re-register from the current checkout. See the [MCP guide](../integrations/mcp/README.md).
- **Missing test browser:** run `node web/node_modules/playwright/cli.js install chromium --only-shell`; on Linux add `--with-deps` if system libraries are missing.
- **Windows:** use the terminal commands with uv/Node on PATH; `.command` is macOS-only. Browser and process behavior should be verified on your Windows environment; automated browser checks run on Linux.
