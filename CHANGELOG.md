# Changelog

## Unreleased

- Add compact question/answer copy controls and a single mixed image/document attachment picker, with draft-scoped upload reservations.
- Consolidate preferences into an editable, user-managed profile with conflict detection. Add searchable, topic-filtered, paginated fact management, source links, inline corrections and selected deletion; retain source boundaries and invalidate edited fact embeddings.

- Add a native macOS desktop window, Dock icon and safe Applications-folder installer without a default Desktop shortcut. Reuse the local server or back up data and start it automatically; support native file selection, exports, keyboard editing and external links. The app uses the existing project and Python environment, with separate WebKit browser storage.

- Standardize public filenames and ordinary developer comments in English. Rename the macOS launcher to `start-learning-tree.command` and the learning-file library to `learning-materials/`; keep legacy learning files accessible in place and excluded from Git. Restart the app after updating to refresh MCP sessions.

- Add local PDF text-layer, DOCX and UTF-8 text document uploads with extraction previews, original downloads and source-linked question attachments. Bound parsing in a separate process and clearly report unsupported scans, encrypted or malformed documents.
- Feed budgeted document excerpts into the current learning path and offer scoped read-only search/pagination; retain attachments across retries and revisions. Export original files with hashes in version 2 backups and reparse/remap them on import, while continuing to read version 1 backups.
- Keep mobile chat accessible when resizing from desktop, with dismissible topic/tree drawers and attachment previews.
- Add per-model context-window settings and local answer reservations. Estimate complete requests before provider calls, including tool definitions, images and tool results; report mandatory inputs that cannot fit.
- Replace the fixed recent-history cutoff with budget-aware retention of complete turns and deterministic extraction of cited original sentences from older context. Preserve stored originals and keep the bounded extraction cache in process memory.
- Offer scoped, read-only original-source pagination for compressed context and tool-enabled turns that may need later compaction. Revalidate the active path at execution; retain tool-call identities and native protocol blocks when fitting requests, and mark omitted history and abbreviated tool results explicitly.
- Keep this context-management path free of extra dependencies, model downloads and additional summarization-model calls. Estimates are not exact provider token counts, and extraction does not guarantee that every relevant detail survives.

## 0.2.0 — 2026-09-12

- Add source-scoped learning-memory retrieval: BM25 and optional multilingual E5 vectors, reciprocal rank fusion, optional BGE reranking, complete-entry budgets and source annotations. User preferences remain separate; sibling branches do not silently supply facts.
- Preserve existing memories and add a rebuildable embedding cache. Memory, branch and tree deletion also remove associated vectors. Publish synthetic inference observations, including known relevance counterexamples.
- Make semantic dependencies and weights optional. Basic `setup` skips PyTorch/Transformers and model downloads; `retrieval` installs the extra on demand. `setup --with-retrieval` retains the usable basic installation if optional preparation fails.
- Show the difference between missing semantic dependencies and unavailable models in Settings → Memory. Normal startup never downloads weights; keyword retrieval remains available.
- Add a synthetic product screenshot and short offline interaction demo to both READMEs, with a no-key trial path and separate personal workspace instructions.
- Branch labels now summarize the first branch question with the selected model in the background. Empty branches use a localized placeholder, and failed summaries fall back to the question.
- Preserve source anchors and custom/imported titles; repair legacy labels that copied the beginning of a source answer.

See [v0.2.0 release and upgrade notes](docs/releases/v0.2.0.md) for installation, resource costs and compatibility.

## 0.1.0 — 2026-09-11

First public LearningTree release.

- Continuous chat, branches, inline explanations, source return and understanding notes.
- Topic map with turn navigation and reversible centering.
- Durable answers, retries, revisions, drafts and JSON import/export.
- OpenAI-compatible Chat Completions and native Anthropic Messages.
- Optional pinned MCP tools with persistent sessions.
- Chinese and English interface with saved language preference and LearningTree branding.
- Portable setup, development and backup commands, isolated browser tests, dependency locks, CI and MIT licensing.
