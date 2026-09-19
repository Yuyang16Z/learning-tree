# Architecture

LearningTree uses React + TypeScript and FastAPI + SQLite/SQLModel. Normal startup builds `web/dist` and serves it from the API's port. Development uses Vite's `/api` proxy to a separate backend and database.

| Area | Responsibility |
| --- | --- |
| `web/src/App.tsx` | Topic selection, conversation path, generation and workspace state. |
| `web/src/components/ChatPane.tsx` | Messages, composer, source selection, explanation, revisions and notes. |
| `web/src/components/LearningMap.tsx` | Topic grouping, navigation and viewport history. |
| `web/src/i18n/` | Locale resolution, persistence and translation helpers. |
| `app/routers/` | Validated endpoints and streaming orchestration. |
| `app/service.py` | Conversation context and learning memory. |
| `app/context.py` | Budget-aware active-path assembly and scoped source rereading. |
| `app/context_budget.py` | Local request-size estimates and protocol-preserving fitting before provider calls. |
| `app/context_compaction.py` | Cited, complete-sentence extraction and a bounded process-local cache. |
| `app/learning_summaries.py`, `app/summary_generation.py` | Source-validated persistent summary blocks, bounded provider requests and local fallback. |
| `app/tool_catalog.py` | Turn-scoped discovery and exact tool-schema loading within the context allowance. |
| `app/documents.py` | Local document storage, bounded parsing, previews and original downloads. |
| `app/retrieval.py` | Source filtering, keyword/vector ranking, fusion, deduplication and memory budgets. |
| `app/semantic_models.py` | Pinned local embedding/reranker models, cached startup and explicit preparation. |
| `app/llm.py`, `app/anthropic_provider.py` | Provider request conversion and deterministic demo behavior. |
| `app/title_generation.py` | Bounded, question-first branch title requests and plain-text fallback. |
| `app/mcp_client.py` | Persistent process/session lifecycle and serialized tool calls. |
| `app/changes.py` | Revisioned, ID-only change notices for other windows and devices. |
| `app/phone_access.py` | Read-only Tailscale status for **Settings → Phone access**. |
| `app/db.py`, `app/models.py` | SQLite initialization, additive upgrades and models. |

## Conversation model

A tree owns nodes. Each node records its parent, kind (`root`, `followup`, `branch`, `revision`), status, source references and learning note. Messages belong to nodes. Legacy multi-turn nodes remain intact; the thread API presents their complete sequence.

Tree titles and archive status are sidebar metadata. `PATCH /trees/{id}` changes either without rewriting node titles, questions, messages or memories. An additive `archived` column defaults to false for existing trees. Normal tree listings exclude archived items; `include_archived=true` includes them for management and source navigation. Archived topics remain accessible and editable, and restoration only clears the flag. Portable JSON exports omit this local organization state, so imports always appear as active topics.

The map groups consecutive follow-ups into topic cards; expanded turns navigate to individual questions. Source node/message IDs and character offsets let branches return to their originating passages. Editing creates a revision instead of overwriting messages.

Branch titles have an independent `title_state`: empty, pending, ai, fallback, manual, or legacy. The first branch question immediately supplies a provisional label; a separate worker asks the same model for a short title. A 20-second settlement deadline ignores late results. Title failures never change answer status. The map polls only pending title metadata, with navigation guards and a 30-second limit, without resetting chat state or the camera. Startup replaces recognizable legacy source-prefix labels with the first question, using no model calls. Title job state is internal and excluded from version-1 exports; imported labels are treated as explicit titles.

Context follows the current ancestry. Short eligible histories remain complete. Longer histories retain recent complete question/answer groups within the selected model's input budget; older sources contribute categorized original sentences with provenance and explicit omission notices. The current question, its images, selected passage and current learning note remain mandatory. Factual memory is restricted to the ancestor path, without silently mixing siblings. Understanding is brought back as an editable message draft.

The question-navigation rail uses each rendered question's node ID plus message
ID, so legacy nodes containing several turns remain independently addressable.
It scrolls inside the current reading pane without loading another node or
calling a model. Selected-text copy also stays entirely local.

Inline **Explain** uses the currently selected model (or the configured default
when no model ID is supplied). It assembles the root-to-selected-node messages,
branch quote and understanding notes through the normal bounded context builder.
It currently does not retrieve global preferences, long-term memory or document
excerpts, and has no MCP tool calls. The short response is transient; it does not
create stored messages or a new branch.

## Question documents

An uploaded document keeps its original bytes, extracted sections and metadata in SQLite. Messages carry document IDs; upload alone makes no provider request. PDF text layers, Word body paragraphs/tables and UTF-8 text files are parsed locally with size and extraction bounds. Parsed text remains reference material; it is not executable code or an independent instruction source.

Question assembly resolves document references on the current path and passes budgeted excerpts into the configured answer model. Eligible originals can be reread in pages through the source reader, whose allowed document IDs are fixed to the current request. Sibling branches do not gain access through a model-supplied ID. This document path is separate from semantic `Memory` retrieval and MCP learning files.

Retry retains the stored question's document references; revision creates another question with its chosen references. Document-bearing tree exports use version 2 with original bytes and integrity hashes. Import validates and reparses those bytes and remaps IDs, while version-1 backups remain supported. See [formats, storage, sending and limits](documents.md).

## Context budgeting and compaction

Each `ModelConfig` stores `context_window` (default 32,768) and `max_tokens` (default 4,096). The local input budget subtracts the answer reserve and `max(512, context_window // 20)` headroom. UTF-8 byte counts, fixed image reservations and protocol overhead approximate request size without downloading tokenizers or calling another model. These estimates do not reproduce provider tokenization or guarantee acceptance by every service.

`context_compaction.py` performs deterministic extractive compaction: it ranks complete source sentences about corrections, conditions, unresolved questions, learning goals, definitions and user notes, then includes fitting excerpts with node/message/section/character references. Partial answers remain stored but are not promoted into these extracts. Historical images omitted from the request are explicitly described as unseen. The algorithm does not invent a free-form summary, rewrite the database or guarantee lossless compression. The disposable process-local LRU holds at most 64 source-extraction entries, keyed by source-content hash and algorithm version; source edits change the cache key.

After final tool budgeting, compressed chat histories can use `learning_summaries.py` to reuse or generate source-scoped summaries of older blocks. At most one new provider call is allowed per question; the rest uses existing summaries or extractive fallback. `summary_generation.py` uses the selected model with no tools, a bounded timeout and zero retries, validating structured learning sections, known source IDs and output size. `ContextSummary` is an additive, rebuildable SQLite cache, independent of long-term memory and original messages. Cache keys include exact source and model identity; reuse and persistence check current sources. Provider work runs outside the mutation lock, with a final locked source validation before writes. Editing notes and deleting sources invalidate dependent rows in the same transaction. Stop events prevent late writes and prevent cancelled requests from starting the answer. Full originals remain the authority; semantic accuracy is not guaranteed by structural validation.

`read_learning_source` offers the answer model a read-only way to page through eligible originals on the active path. It is enabled when initial context has been compressed, or when the user selected tools whose later results may require runtime compaction. Short requests without user-selected tools retain the plain streaming path. Reader execution revalidates the current path and source instead of trusting the model's supplied IDs. This is separate from MCP and semantic memory retrieval. It does not expose sibling branches, another tree or arbitrary files.

Before each answer-provider request, including continuation after a tool result, `fit_request` estimates the full request with system text, messages, images and tool definitions. It drops older history as complete protocol groups when necessary, adds an explicit omission notice with a source-rereading reminder, and preserves the current question and current tool-call IDs, arguments and native signed blocks. Oversized tool-result bodies may retain only the beginning and end, with an explicit omission warning. Rereading a source must not be implemented by replaying a side-effecting tool action. If mandatory input cannot fit, the request fails with actionable guidance instead of silently cutting it.

OpenAI-compatible requests keep their existing wire behavior without a new output-token cap: `max_tokens` supplies the local answer reservation, while actual output length remains provider-controlled. Native Anthropic requests still send `max_tokens` as their output limit. See [configuration, examples and limitations](context-budget.md).

## Learning memory retrieval

Raw messages and extracted `Memory` records are separate stores. Successful, non-demo completed answers can trigger background extraction of user preferences and topic facts. An interrupted answer may remain in message history, but does not enter that automatic extraction path. A fact is a model-extracted learning conclusion, not an externally verified claim.

For each question, the built-in memory pipeline runs as follows:

1. **Filter sources before ranking.** Topic candidates must belong to the current tree and allowed ancestor/current-node sources. Missing, pending, failed and interrupted sources are excluded. Sibling facts never enter the embedding or reranking candidate set.
2. **Form a query from the question and selected passage.** BM25 matches English terms and Chinese character bigrams, including technical notation. Local `intfloat/multilingual-e5-small` embeddings provide a complementary semantic ranking, using E5's `query:` and `passage:` prefixes and normalized vectors. Retrieval considers all eligible memory candidates before ranking; it does not first discard older entries by recency.
3. **Fuse and optionally rerank.** Reciprocal rank fusion (RRF) combines keyword and vector rankings into a bounded candidate set. The local `BAAI/bge-reranker-v2-m3` scores query–memory pairs when reranking is needed and available. Similarity and relevance filters can return no facts when nothing qualifies; reranker scores are neither calibrated relevance probabilities nor factual confidence scores. Weakly related text can still survive filtering; the [synthetic validation](memory-retrieval-validation.md) preserves concrete counterexamples.
4. **Deduplicate and budget complete entries.** Normalize duplicate memory text and omit facts already present in the supplied conversation context. Select complete entries within a character budget; an entry that would exceed the budget is skipped rather than cut through a negation or precondition. This is a character budget, not a tokenizer-wide context-window guarantee.
5. **Revalidate and attach provenance.** Recheck selected records against current stored content and source eligibility before assembling the memory note. Each entry carries its memory ID and source node. The note labels historical material as potentially wrong or outdated and tells the answer model to prioritize the user's current explicit request.

User preferences are handled separately: valid recent preferences are deduplicated and placed in their own small budget, without requiring semantic similarity to the current technical question. Memory retrieval supplements the budgeted path context; it does not determine which original messages are preserved in the database or replace the current question and selected source passage. The final request budget also accounts for the memory note, selecting or omitting whole entries including multiline conditions. Deduplication against supplied context uses the mandatory quotation and current note, avoiding the loss of a memory solely because it duplicated older history that was subsequently compressed away.

After the user explicitly saves their editable preference profile, the additive `PreferenceProfile` singleton supersedes automatically extracted preferences, including when empty. Automatic extraction continues for facts but cannot rewrite that profile. Its revision detects concurrent edits; a clear-all epoch prevents in-flight extraction from restoring cleared content. Context assembly reserves the complete bounded profile before older turns when possible, otherwise omitting it whole to preserve mandatory current inputs. Fact edits retain their original source, compare the expected content and invalidate cached vectors. Management search and pagination do not change retrieval eligibility. See [memory management](memory-management.md).

`MemoryEmbedding` is an additive SQLite cache of normalized document vectors. Its composite key identifies the memory and pinned embedding-model revision, and a content hash detects stale text. Existing `Memory` IDs and content remain unchanged. Missing vectors are rebuilt lazily from eligible records; deleted memories, branches and trees remove associated vectors. SQLite remains the authority for eligibility, not the vector cache. This local implementation scans eligible cached vectors without requiring a separate vector database.

Semantic Python dependencies belong to the optional `retrieval` extra. Basic `manage.py setup` does not install PyTorch/Transformers or prepare weights; `manage.py retrieval` installs that extra and explicitly prepares models. `setup --with-retrieval` combines both stages, retaining a usable basic installation if the optional stage fails. Installed extras are preserved during ordinary managed setup and startup.

Model weights are pinned by repository revision, loaded as safetensors on CPU with remote model code disabled, and cached under `.runtime/retrieval/models/`. E5-small uses revision `614241f622f53c4eeff9890bdc4f31cfecc418b3` (MIT); BGE-reranker-v2-m3 uses `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` (Apache-2.0). Their combined weights use approximately 2.55 GiB, excluding tokenizers and Python dependencies. With optional dependencies installed, `scripts/prepare_retrieval.py` explicitly prepares/downloads weights without installing packages. Normal server startup calls `warm_cached()` in the background with downloads disabled; query-time inference never downloads. Missing or failing embeddings use the keyword path. If only reranking is unavailable, the fused keyword/vector ranking remains usable.

`GET /api/memories/retrieval/status` returns `state` (`not_installed`, `ready`, `preparing`, `degraded`, `disabled`), `embedding_ready` and `reranker_ready`, without private paths or raw model errors. Missing optional packages produce `not_installed`; Settings → Memory then shows the CLI installation command. With dependencies installed, `POST /api/memories/retrieval/prepare` starts weight preparation in the background, and the panel offers retry. The API does not install Python packages. Polling stops after a bounded interval or when the panel closes. `MEMORY_RETRIEVAL_MODE=lexical` disables semantic models.

This pipeline only searches built-in `Memory` records. It does not index full conversation archives, learning files or the independent MCP knowledge graph. Deleting extracted memory leaves original chat history intact, so that history can still be included when it lies on the current path. Retrieval inference is local; the selected memory note becomes part of the configured answer model's prompt. Relevant retrieval is not a truth check, conflict-resolution system or automatic forgetting policy.

## Generation and recovery

The backend persists a pending turn before streaming and uses request IDs to avoid duplicate submission. After acceptance, the composer clears the submitted text; an unaccepted failure retains it. New typing during generation survives automatic navigation.

Server-sent events carry updates. An answer runs in a server worker, not in the request that started it: closing the page, reloading or a phone locking only disconnects that viewer. Every event is kept in order for the running answer, so `GET /nodes/{id}/stream?request_id=…&after=N` reattaches and replays only events after `N`; once the answer has ended it reports the stored outcome instead. The browser reattaches automatically after a dropped connection while the page is visible, keeping the text it already shows. Only **Stop**, deleting the node or stopping the server ends an answer. Memory extraction runs after completion even when nobody is connected.

Partial output is checkpointed about every half second, retained after interruption or failure and can be inspected after retrying. On startup, leftover pending nodes become interrupted. Only one backend should use a database at a time. Stop prevents subsequent local steps, but cannot undo a remote request or tool action already executed.

## Other windows and devices

Every client of one backend shares its database, models and MCP configuration, so there is nothing to synchronize between copies. `app/changes.py` records which rows each committed transaction touched: node IDs grouped by topic, and flags for the topic list, models, MCP servers and memory. `GET /changes?since=<cursor>` returns what changed after the cursor; an unknown cursor (new page, restarted server) or one older than the retained history sets `reload`. Notices carry IDs only; pages reload changed data through the normal endpoints and keep unchanged state untouched.

Visible pages poll about every two seconds, stop while hidden, and poll immediately when shown, focused or back online. A page reloads the map when its topic changed and the conversation only when a node on the open path changed, never interrupting its own streaming answer or an in-progress navigation. A question another client is answering shows its checkpointed text with a live status and Stop, and follow-up questions on that node wait until it finishes. Drafts, reading positions, theme and language remain per browser.

The API still has no authentication. Reaching it from another device requires a private network path that you control; see [security policy](../SECURITY.md).

OpenAI-compatible Chat Completions and Anthropic Messages use distinct message, image, stream and tool formats. Mock responses exercise local interactions without paid calls; they do not validate a remote model's capabilities.

## Tools and persistence

The composer toggles select available capabilities; one MCP server can expose many tools. Small tool sets continue to use their original direct definitions. When all selected schemas would exceed their allowance, a turn-local catalog exposes a compact search tool and retains the scoped conversation/document readers. Searching only discovers definitions from the selected set; it does not execute the tools. The catalog loads complete original argument schemas for subsequent requests. Its schema allowance normally targets one third of the input budget, with a minimum sufficient for the pinned readers and discovery definition; it is always capped by the space remaining after mandatory inputs and tool-result headroom. A single definition that cannot fit is reported as unavailable within the current budget instead of silently altering its required arguments.

Each model round is fitted with that round's active definitions. Tool execution is restricted to the definitions advertised in the request that produced the call, so discovery cannot authorize an unselected tool or retroactively authorize another call in the same response. Tool results keep their call IDs and Anthropic native blocks. When earlier derived context crowds out results, fitting can fall back to the protected current inputs rather than discarding the current question, quotation or note. Original stored messages and tool outputs are not rewritten. After the bounded tool rounds, one final request without tools synthesizes the collected evidence; executed actions are not replayed to recover omitted text.

MCP sessions persist between calls, preserving browser/thinking state; calls into a session are serialized. A timed-out or uncertain execution is not blindly replayed. Shutdown closes the runtime. Optional dependencies live under `integrations/mcp/node_modules` and `.runtime/mcp-python`. Registration saves machine-specific paths in SQLite; moving the project requires re-registration.

The managed `web-research` preset exposes `web_search` and `read_webpage` through one MCP session and one composer selection. Search shares the legacy DDGS/Tavily implementation; reading reuses the pinned fetch package's extraction and robots checks with bounded, paginated output. The composer suppresses redundant built-in search/fetch selections while this preset is enabled. The legacy tools remain available when it is absent or disabled. Registration upgrades the old managed `fetch` record in place, preserving its ID, custom label and enabled state. Only the exact local combined launcher receives the optional search credential; testing, execution and session invalidation use the same launch specification.

The file preset exposes `learning-materials/` and the legacy `学习资料/` directory when present, without moving existing files; MCP memory and browser output live in `mcp-data/`. SQLite contains conversations, document originals and extracted sections, configurations and learning memory. Browser storage contains drafts, positions and the `learning-tree.locale` preference (`zh-CN` or `en`). React context updates labels without remounting the workspace; user content is not translated.

Schema upgrades are additive and idempotent. Future complex transformations should use versioned migrations with restore tests. JSON exports retain the `branch-learning` format identifier, use version 2 when documents are present and version 1 otherwise, remap IDs on import, and exclude model credentials and MCP definitions.

## Verification

Backend tests cover ancestry, recovery, import/export, provider conversion, search errors, MCP runtime and memory retrieval boundaries. Retrieval regression tests use controlled model fixtures for ranking and failure cases. Frontend tests cover streams, drafts, map state and localization. Browser smoke tests use a temporary database, deterministic answer model and random loopback port; model-setup UI transitions use route fixtures rather than real downloads. `manage.py check` and `e2e` force lexical mode and use isolated databases, without contacting model services. CI installs committed lockfiles and checks both layers.

The document browser smoke uses generated text/Markdown files to check removal, upload progress, previews, exact original downloads, sending, reload, follow-ups, revision and version-2 export/import. PDF/Word parsing and document context/recovery rules are checked separately in backend tests; browser mock answers do not demonstrate document comprehension.

After the optional `retrieval` extra is installed, `uv run python scripts/prepare_retrieval.py --smoke` separately runs actual local embedding and reranking models on fixed synthetic Chinese and English examples. It may download missing public weights during preparation, but never reads the chat database or calls an answer-model API. This is an inference smoke check, not a comprehensive retrieval or answer-quality evaluation.
