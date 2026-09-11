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
| `app/llm.py`, `app/anthropic_provider.py` | Provider request conversion and deterministic demo behavior. |
| `app/mcp_client.py` | Persistent process/session lifecycle and serialized tool calls. |
| `app/db.py`, `app/models.py` | SQLite initialization, additive upgrades and models. |

## Conversation model

A tree owns nodes. Each node records its parent, kind (`root`, `followup`, `branch`, `revision`), status, source references and learning note. Messages belong to nodes. Legacy multi-turn nodes remain intact; the thread API presents their complete sequence.

The map groups consecutive follow-ups into topic cards; expanded turns navigate to individual questions. Source node/message IDs and character offsets let branches return to their originating passages. Editing creates a revision instead of overwriting messages.

Context follows the current ancestry. The nearest six ancestors retain full text and images; older content is compacted with an omission marker. The selected passage is included. Factual memory is restricted to the ancestor path, without silently mixing siblings. Understanding is brought back as an editable message draft.

## Generation and recovery

The backend persists a pending turn before streaming and uses request IDs to avoid duplicate submission. After acceptance, the composer clears the submitted text; an unaccepted failure retains it. New typing during generation survives automatic navigation.

Server-sent events carry updates. Partial output is retained after interruption or failure and can be inspected after retrying. On startup, leftover pending nodes become interrupted. Only one backend should use a database at a time. Stop prevents subsequent local steps, but cannot undo a remote request or tool action already executed.

OpenAI-compatible Chat Completions and Anthropic Messages use distinct message, image, stream and tool formats. Mock responses exercise local interactions without paid calls; they do not validate a remote model's capabilities.

## Tools and persistence

MCP sessions persist between calls, preserving browser/thinking state; calls into a session are serialized. A timed-out or uncertain execution is not blindly replayed. Shutdown closes the runtime. Optional dependencies live under `integrations/mcp/node_modules` and `.runtime/mcp-python`. Registration saves machine-specific paths in SQLite; moving the project requires re-registration.

The file preset exposes `学习资料/`; MCP memory and browser output live in `mcp-data/`. SQLite contains conversations, configurations and learning memory. Browser storage contains drafts, positions and the `learning-tree.locale` preference (`zh-CN` or `en`). React context updates labels without remounting the workspace; user content is not translated.

Schema upgrades are additive and idempotent. Future complex transformations should use versioned migrations with restore tests. JSON exports retain the `branch-learning` version-1 identifier for compatibility, remap IDs on import, and exclude model credentials and MCP definitions.

## Verification

Backend tests cover ancestry, recovery, import/export, provider conversion, search errors and MCP runtime. Frontend tests cover streams, drafts, map state and localization. Browser smoke tests use a temporary database, deterministic model and random loopback port. CI installs committed lockfiles and checks both layers.
