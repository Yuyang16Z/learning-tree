# Local operation

Run commands from the repository root with uv and Node.js 24 installed.

| Command | Purpose |
| --- | --- |
| `uv run python scripts/manage.py setup` | Install basic dependencies from lockfiles; no PyTorch, Transformers or model-weight download. Preserve any optional dependencies already installed. |
| `uv run python scripts/manage.py setup --with-retrieval` | Install the application, then optional semantic dependencies and weights. A retrieval-preparation failure warns without failing the completed basic installation. |
| `uv run python scripts/manage.py retrieval` | Install or retry the optional semantic dependencies and weights; returns a failure status if preparation cannot complete. |
| `uv run python scripts/manage.py start --open` | Back up data, build and start on `127.0.0.1:8099`; open an existing app if already running. |
| `uv run python scripts/manage.py dev` | Isolated development on 8100/5174 with `.runtime/dev.db`; initially seeds a demo model, retains later user configurations, and uses lexical retrieval. |
| `uv run python scripts/manage.py backup` | Online SQLite backup to `.backups/`. |
| `uv run python scripts/manage.py check` | Lint, format, unit/regression tests and frontend build; temporary test database and lexical retrieval. |
| `uv run python scripts/manage.py e2e` | UI checks with a temporary database, random local port and lexical retrieval. |
| `uv run python scripts/prepare_retrieval.py --smoke` | With optional dependencies installed, prepare/download weights and verify real inference on synthetic Chinese/English examples; fails if dependencies or models are unavailable. |

An occupied port is not forcibly freed. Ctrl+C stops the app. Use one backend per database; the standard launcher guards its port, but cannot police custom commands on other ports.

## Data locations

| Location | Contains |
| --- | --- |
| `branch_learning.db` | Normal chats, model keys, notes, memory, user-managed `PreferenceProfile`, rebuildable `MemoryEmbedding` vectors and tool configuration. |
| `.runtime/dev.db` | Separate development data and demo configuration. |
| `.runtime/retrieval/models/` | Pinned public embedding/reranker weights; no chat records. |
| `.backups/` | Private database copies including credentials. |
| `learning-materials/` | Files exposed to the optional file tool; only its public README is tracked. |
| `学习资料/` (legacy) | Existing learning files remain accessible in place after upgrading and stay excluded from Git. |
| `mcp-data/` | MCP knowledge graph and browser artifacts. |
| Browser storage | Drafts, positions, language and a content-free deletion notice for other open tabs. |

These local data files are excluded from Git. `DATABASE_URL` may select another SQLite file. Supplied commands resolve relative paths from the project root.

## Local memory retrieval

The basic installation uses keyword retrieval and needs no model weights. The default `hybrid` configuration adds vectors and reranking when their optional dependencies and models are available; it does not require a separate API key. To enable them:

```sh
uv run python scripts/manage.py retrieval
```

The command installs the `retrieval` extra, then downloads pinned multilingual E5-small and BGE-reranker-v2-m3 weights. Allow approximately 2.55 GiB for weights, plus tokenizers and runtime dependencies. Both models run on CPU with two PyTorch compute threads; loaded models also consume memory, so weight-file size is not a RAM limit. See [measured local costs and relevance limitations](memory-retrieval-validation.md). Restart the app after installing dependencies. This explicit preparation temporarily enables hybrid mode for its subprocess but does not rewrite `.env`: if you previously selected `MEMORY_RETRIEVAL_MODE=lexical`, change it to `hybrid` to enable semantic retrieval in the app. Normal startup only warms cached models in the background; neither startup nor answering a question downloads missing weights.

Open **Settings → Memory** to inspect readiness. If optional dependencies are missing, the panel shows the installation command above. Once dependencies are installed, **Prepare / retry** explicitly starts a background weight download/load attempt. While preparation runs, or if embeddings are unavailable, answers can continue using keyword retrieval. If embeddings are ready but the reranker is unavailable, hybrid retrieval still works without the second-stage rerank. Local retrieval does not send chat content to a retrieval service; selected memories are still included in prompts sent to the configured answer model.

The management API uses `GET /api/memories/retrieval/status` for read-only status and `POST /api/memories/retrieval/prepare` to prepare models. Both return `state`, `embedding_ready` and `reranker_ready`; `not_installed` distinguishes missing optional Python components from unavailable weights. Neither endpoint installs Python packages, and the status route never downloads models. The settings panel polls briefly during preparation, then offers manual refresh if the background work takes longer.

For keyword-only operation, set `MEMORY_RETRIEVAL_MODE=lexical` in `.env` and restart. This disables semantic loading even when optional dependencies are installed. Development, unit/regression checks and browser tests force lexical mode in their isolated environment and do not download weights. For real local model validation after installation, use `prepare_retrieval.py --smoke` with hybrid mode enabled. It uses synthetic examples and never opens the chat database; success does not establish real-answer quality or factual correctness.

Ordinary `uv run` commands and the setup command preserve installed optional dependencies. If you maintain the environment with a manual exact `uv sync`, include `--extra retrieval` to retain the semantic packages, or use `--inexact` to preserve additional packages. Removing Python dependencies does not delete existing model weights or chat records.

Existing memories do not need to be imported again. Their IDs and content are unchanged, and missing embedding-cache entries are generated lazily during retrieval. The cache is derived data: a restored database without these vectors can rebuild them from its memory records. A missing model-weight cache is restored by preparation. If you prepare weights with the CLI while the app is already running, use **Prepare / retry** in that process or restart to load them.

Deleting one memory, clearing memory, or deleting a source branch/tree also removes associated cached vectors. Deleting an extracted memory does not delete the original chat messages, which may still enter the current conversation's context. These controls do not clear MCP memory or index/delete files in `learning-materials/` or the legacy `学习资料/` directory; those are independent stores.

## Model context settings

In **Settings → Models & API keys → add/edit a model → Advanced**, set **Context window** to the capacity actually supported by that provider and model. The default is 32,768, with a 4,096 answer reserve and additional estimation headroom. These values are stored per model, not in `.env`; existing configurations receive the default window without changing their credentials or chat records.

For OpenAI-compatible models, **Answer reserve** reduces the local input allowance but does not impose a new provider output cap. For native Anthropic, **Max output tokens** also remains the output limit sent to the provider. Local estimates use UTF-8 byte counts and fixed image reservations, not the provider's exact tokenizer.

Context management requires no optional semantic dependencies or model downloads. Short histories remain complete. Long chat histories keep recent full turns and use cached summaries plus cited original sentences from older sources. Each question may make at most one extra, potentially billable summary call using the selected answer model. Summary failures fall back to local extraction. Source rereading is available when initial context is compressed, or when selected tools may cause later budget pressure. Stored messages remain intact. The local extraction cache clears on restart; the separate `ContextSummary` cache persists in SQLite and is rebuildable. Editing or deleting sources invalidates dependent summaries. Database backups include this cache, but tree JSON exports omit it.

Enabling an MCP server makes its tools available; it can expose many individual tools. Large selected sets now use on-demand discovery: the model searches the enabled tools and loads the complete definitions it needs within a separate schema allowance. Discovery alone does not execute a tool or enable unselected servers. Small sets remain directly available. Original-source readers stay available for the current path and attached documents. After the bounded tool rounds, the model makes one final answer attempt from the collected evidence without executing more tools.

If the app reports that a question, quotation, image or individual tool definition exceeds the budget, shorten the mandatory input, reduce attached images, use a smaller relevant tool, or correct the configured window if the provider supports more. An oversized individual schema is reported when requested; enabling a large server no longer automatically sends every definition in every model request. Raising the setting beyond the provider's capacity cannot make the model accept it. Earlier derived context and tool-result bodies may be explicitly shortened in later requests, while stored originals remain intact. See [the context-budget guide](context-budget.md) for source references, protocol handling and compression limits.

## Phone access

A phone uses this computer's backend directly, so records, models and MCP settings are shared without any synchronization. The API has no sign-in: reach it only through a private network you control.

1. Install [Tailscale](https://tailscale.com/download) on this computer and the phone, and sign in to the same account.
2. Run `tailscale serve --bg 8099` once; the setting persists. It serves `https://<computer>.<tailnet>.ts.net` with a certificate, only to devices in your tailnet. HTTPS is required: the app uses browser features that exist only in secure contexts, so a plain `http://` network address cannot send questions.
3. Open **Settings → Phone access** and scan the QR code. The panel only reads Tailscale status; it never changes it.

Keep the computer awake while you use it remotely, for example by preventing automatic sleep on the power adapter; a sleeping computer cannot answer. An answer started on the phone keeps running if the phone locks, and the page reconnects when it returns to the foreground. Do not use Tailscale Funnel, router port forwarding or public tunnels: anyone who reaches the API can read your records and run MCP programs on this computer. The panel warns when Funnel exposes the app; `tailscale funnel reset` removes the exposure.

## Updating and restoring

1. Let active answers finish and stop the app.
2. Run `uv run python scripts/manage.py backup`.
3. Pull the update; run `uv run python scripts/manage.py setup`.
4. Start again and refresh the browser. Additive upgrades preserve existing IDs and records.

The macOS launcher is now `start-learning-tree.command`. New learning files belong in `learning-materials/`. If the old `学习资料/` directory exists, the file MCP also exposes it in place; the upgrade never moves, merges or overwrites private files. Both libraries stay excluded from Git apart from `learning-materials/README.md`. Restart the app after updating so cached MCP sessions reload the directory list.

For a tagged version, see the corresponding [release notes](releases/v0.2.0.md). Basic setup preserves previously installed semantic dependencies and existing model caches. New users can add semantic retrieval separately; upgrading does not require downloading models merely to use chat or keyword memory.

For full recovery, stop the app, keep a copy of the current data file, and copy a chosen private backup to the configured database path. Start one backend. A database restore does not restore browser drafts, learning files or MCP memory; back those up separately when moving machines.

Tree JSON import creates a new tree and remaps references. Limits are 25 MiB, 2,000 nodes and 12,000 messages. Backups containing question documents use version 2, include original bytes and integrity hashes, and are reparsed on import; version-1 backups remain supported. Embedded originals count toward the JSON limit, so large collections may need a private SQLite backup. Automatic preference/factual memory, the user-managed preference profile and MCP memory are not included. Exported conversations and documents can still be private even though model keys are excluded. See [document formats and data flow](documents.md).

## Troubleshooting

Topics can be renamed, archived or deleted from their sidebar **…** menu. Archiving preserves all records and hides the topic from **My topics**. Open **Settings → Archived** to read an archived topic; opening it closes Settings without restoring it. Its **…** menu supports renaming, restoring and deleting. Restoring returns the topic to the sidebar while keeping Settings open. This state lives in SQLite and is shared between the browser and desktop app; exported JSON omits it and imports appear as active topics. Renaming a topic does not rewrite its original root question.

Confirming deletion removes the tree, its nodes and messages (including images),
stored document originals and extracted sections, source-linked memories and
their cached vectors. The interface clears that tree's local drafts and reading
positions after the server confirms deletion. It no longer exports a recovery
snapshot or offers **Restore last deletion**; upgrading also removes the old
browser recovery snapshot. Use **Archive** for topics you may want to reopen.

Deleting only a branch removes its messages and source-linked memories. Uploaded
documents belong to the entire topic and can be shared by surviving branches;
the document library is removed when the whole topic is deleted.

Deletion operates on data owned by that tree in the live application. Independently
exported files, historical whole-database backups, the user-managed preference
profile and shared MCP libraries/knowledge/browser artifacts have separate
lifecycles. They are not attributed to individual trees and are not removed by
matching topic names. Existing whole-database backups can still contain older
records; this operation is not a secure erase of backup media.

- **No model:** add one in Settings, or run `manage.py dev` for the offline demo.
- **Old UI:** refresh after rebuilding/restarting. Clearing browser storage also removes drafts and preferences.
- **Document upload rejected:** use a supported text format, a text-layer PDF or a `.docx` file within the documented limits. Convert plain text to UTF-8, decrypt protected files, or split large inputs. Scanned pages require a separate OCR step; see [document limits](documents.md#formats-and-limits).
- **Context budget exceeded:** review the model's Advanced settings, then reduce oversized mandatory inputs or use a smaller relevant tool if its individual schema cannot fit. Large selected tool sets load definitions on demand. See [model context settings](#model-context-settings). A provider can still reject a request that passes the local estimate because tokenization and image accounting differ.
- **Semantic retrieval is not installed:** run `uv run python scripts/manage.py retrieval` and restart. This installs optional Python packages as well as weights; the settings page does not install Python packages.
- **Semantic models are unavailable:** after installing the optional packages, use **Settings → Memory → Prepare / retry**, or run `uv run python scripts/prepare_retrieval.py --smoke`. Preparation needs access to public Hugging Face model files. If a network or model error occurs, basic chat and keyword retrieval remain available. Retry later; no new model API key is required.
- **MCP path errors:** install dependencies and re-register from the current checkout. See the [MCP guide](../integrations/mcp/README.md).
- **Missing test browser:** run `node web/node_modules/playwright/cli.js install chromium --only-shell`; on Linux add `--with-deps` if system libraries are missing.
- **Windows:** use the terminal commands with uv/Node on PATH; `.command` is macOS-only. Browser and process behavior should be verified on your Windows environment; automated browser checks run on Linux.
