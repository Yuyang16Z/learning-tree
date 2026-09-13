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
| `app/retrieval.py` | Source filtering, keyword/vector ranking, fusion, deduplication and memory budgets. |
| `app/semantic_models.py` | Pinned local embedding/reranker models, cached startup and explicit preparation. |
| `app/llm.py`, `app/anthropic_provider.py` | Provider request conversion and deterministic demo behavior. |
| `app/title_generation.py` | Bounded, question-first branch title requests and plain-text fallback. |
| `app/mcp_client.py` | Persistent process/session lifecycle and serialized tool calls. |
| `app/db.py`, `app/models.py` | SQLite initialization, additive upgrades and models. |

## Conversation model

A tree owns nodes. Each node records its parent, kind (`root`, `followup`, `branch`, `revision`), status, source references and learning note. Messages belong to nodes. Legacy multi-turn nodes remain intact; the thread API presents their complete sequence.

The map groups consecutive follow-ups into topic cards; expanded turns navigate to individual questions. Source node/message IDs and character offsets let branches return to their originating passages. Editing creates a revision instead of overwriting messages.

Branch titles have an independent `title_state`: empty, pending, ai, fallback, manual, or legacy. The first branch question immediately supplies a provisional label; a separate worker asks the same model for a short title. A 20-second settlement deadline ignores late results. Title failures never change answer status. The map polls only pending title metadata, with navigation guards and a 30-second limit, without resetting chat state or the camera. Startup replaces recognizable legacy source-prefix labels with the first question, using no model calls. Title job state is internal and excluded from version-1 exports; imported labels are treated as explicit titles.

Context follows the current ancestry. The nearest six ancestors retain full text and images; older content is compacted with an omission marker. The selected passage is included. Factual memory is restricted to the ancestor path, without silently mixing siblings. Understanding is brought back as an editable message draft.

## Learning memory retrieval

Raw messages and extracted `Memory` records are separate stores. Successful, non-demo completed answers can trigger background extraction of user preferences and topic facts. An interrupted answer may remain in message history, but does not enter that automatic extraction path. A fact is a model-extracted learning conclusion, not an externally verified claim.

For each question, the built-in memory pipeline runs as follows:

1. **Filter sources before ranking.** Topic candidates must belong to the current tree and allowed ancestor/current-node sources. Missing, pending, failed and interrupted sources are excluded. Sibling facts never enter the embedding or reranking candidate set.
2. **Form a query from the question and selected passage.** BM25 matches English terms and Chinese character bigrams, including technical notation. Local `intfloat/multilingual-e5-small` embeddings provide a complementary semantic ranking, using E5's `query:` and `passage:` prefixes and normalized vectors. Retrieval considers all eligible memory candidates before ranking; it does not first discard older entries by recency.
3. **Fuse and optionally rerank.** Reciprocal rank fusion (RRF) combines keyword and vector rankings into a bounded candidate set. The local `BAAI/bge-reranker-v2-m3` scores query–memory pairs when reranking is needed and available. Similarity and relevance filters can return no facts when nothing qualifies; reranker scores are neither calibrated relevance probabilities nor factual confidence scores. Weakly related text can still survive filtering; the [synthetic validation](memory-retrieval-validation.md) preserves concrete counterexamples.
4. **Deduplicate and budget complete entries.** Normalize duplicate memory text and omit facts already present in the supplied conversation context. Select complete entries within a character budget; an entry that would exceed the budget is skipped rather than cut through a negation or precondition. This is a character budget, not a tokenizer-wide context-window guarantee.
5. **Revalidate and attach provenance.** Recheck selected records against current stored content and source eligibility before assembling the memory note. Each entry carries its memory ID and source node. The note labels historical material as potentially wrong or outdated and tells the answer model to prioritize the user's current explicit request.

User preferences are handled separately: valid recent preferences are deduplicated and placed in their own small budget, without requiring semantic similarity to the current technical question. Memory retrieval supplements the existing path context; it does not replace recent full question/answer turns, selected source passages or learning notes.

`MemoryEmbedding` is an additive SQLite cache of normalized document vectors. Its composite key identifies the memory and pinned embedding-model revision, and a content hash detects stale text. Existing `Memory` IDs and content remain unchanged. Missing vectors are rebuilt lazily from eligible records; deleted memories, branches and trees remove associated vectors. SQLite remains the authority for eligibility, not the vector cache. This local implementation scans eligible cached vectors without requiring a separate vector database.

Model weights are pinned by repository revision, loaded as safetensors on CPU with remote model code disabled, and cached under `.runtime/retrieval/models/`. E5-small uses revision `614241f622f53c4eeff9890bdc4f31cfecc418b3` (MIT); BGE-reranker-v2-m3 uses `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` (Apache-2.0). Their combined weights use approximately 2.55 GiB, excluding tokenizers and Python dependencies. `manage.py setup` and `scripts/prepare_retrieval.py` explicitly prepare/download weights. Normal server startup calls `warm_cached()` in the background with downloads disabled; query-time inference never downloads. Missing or failing embeddings use the keyword path. If only reranking is unavailable, the fused keyword/vector ranking remains usable.

`GET /api/memories/retrieval/status` returns `state` (`ready`, `preparing`, `degraded`, `disabled`), `embedding_ready` and `reranker_ready`, without private paths or raw model errors. `POST /api/memories/retrieval/prepare` starts preparation in the background. Settings → Memory displays this state and lets the user retry; polling stops after a bounded interval or when the panel closes. `MEMORY_RETRIEVAL_MODE=lexical` disables semantic models.

This pipeline only searches built-in `Memory` records. It does not index full conversation archives, learning files or the independent MCP knowledge graph. Deleting extracted memory leaves original chat history intact, so that history can still be included when it lies on the current path. Retrieval inference is local; the selected memory note becomes part of the configured answer model's prompt. Relevant retrieval is not a truth check, conflict-resolution system or automatic forgetting policy.

## Generation and recovery

The backend persists a pending turn before streaming and uses request IDs to avoid duplicate submission. After acceptance, the composer clears the submitted text; an unaccepted failure retains it. New typing during generation survives automatic navigation.

Server-sent events carry updates. Partial output is retained after interruption or failure and can be inspected after retrying. On startup, leftover pending nodes become interrupted. Only one backend should use a database at a time. Stop prevents subsequent local steps, but cannot undo a remote request or tool action already executed.

OpenAI-compatible Chat Completions and Anthropic Messages use distinct message, image, stream and tool formats. Mock responses exercise local interactions without paid calls; they do not validate a remote model's capabilities.

## Tools and persistence

MCP sessions persist between calls, preserving browser/thinking state; calls into a session are serialized. A timed-out or uncertain execution is not blindly replayed. Shutdown closes the runtime. Optional dependencies live under `integrations/mcp/node_modules` and `.runtime/mcp-python`. Registration saves machine-specific paths in SQLite; moving the project requires re-registration.

The file preset exposes `学习资料/`; MCP memory and browser output live in `mcp-data/`. SQLite contains conversations, configurations and learning memory. Browser storage contains drafts, positions and the `learning-tree.locale` preference (`zh-CN` or `en`). React context updates labels without remounting the workspace; user content is not translated.

Schema upgrades are additive and idempotent. Future complex transformations should use versioned migrations with restore tests. JSON exports retain the `branch-learning` version-1 identifier for compatibility, remap IDs on import, and exclude model credentials and MCP definitions.

## Verification

Backend tests cover ancestry, recovery, import/export, provider conversion, search errors, MCP runtime and memory retrieval boundaries. Retrieval regression tests use controlled model fixtures for ranking and failure cases. Frontend tests cover streams, drafts, map state and localization. Browser smoke tests use a temporary database, deterministic answer model and random loopback port; model-setup UI transitions use route fixtures rather than real downloads. `manage.py check` and `e2e` force lexical mode and use isolated databases, without contacting model services. CI installs committed lockfiles and checks both layers.

`uv run python scripts/prepare_retrieval.py --smoke` separately runs actual local embedding and reranking models on fixed synthetic Chinese and English examples. It may download missing public weights during preparation, but never reads the chat database or calls an answer-model API. This is an inference smoke check, not a comprehensive retrieval or answer-quality evaluation.
