# Changelog

## Unreleased

- Add `scripts/evaluate_retrieval.py` to measure, on a temporary copy of your own database, how the character-class estimator compares with the earlier UTF-8 byte count (including how many learning paths would compact with 32,768- and 1,048,576-token windows), what memory retrieval returns for every completed question in the path, other-branch and all-topic scopes, replayed against the facts that existed when it was asked and scored with proxy recall@k and MRR rather than human relevance labels, and how topic search finds each question. The original file is never opened for writing, no model is called and nothing is downloaded; the report in `artifacts/` holds ids, counts, scores and timings without paths or conversation text unless `--include-text` is given, `--hybrid` exits with status 2 unless both local models (embedding and reranking) are prepared, at least one question reaches them and no retrieval falls back from either, and `--synthetic` runs a built-in fixture. See [offline retrieval evaluation](docs/retrieval-evaluation.md).
- Preload the managed web-research MCP's `web_search` and `read_webpage` (under their `mcp_<id>_` names) in the on-demand tool catalog exactly like the built-in web tools, so a first model round can search or read the web without a discovery call; look-alike names stay searchable only.
- Assemble the system prompt stable-first: fixed rules, the ancestor learning path with notes and imported summaries, then the current node, note, quoted passage, document excerpts and finally retrieved memories. For native Anthropic models, send that stable head as a cached text block with the top-level cache marker when it is at least 1,024 characters and still a prefix of the fitted prompt; a provider that rejects the marker gets one plain retry and the marker is then disabled for that endpoint. OpenAI-compatible requests are unchanged, and cached tokens appear in the answer diagnostics.
- Stream tool rounds: text and visible reasoning the model produces before a tool call now appear as they arrive, tool steps show once the message is complete and its stop reason accepted, and the final answer is relayed as it streams instead of being replayed in small chunks. OpenAI-compatible rounds assemble streamed tool-call fragments by index and parse the arguments only at the end, so partial calls never execute.
- Compact older history once it exceeds `CONTEXT_COMPACTION_RATIO` (default 0.6, `.env`) of the input budget, so follow-ups and tool results keep room; when tools reserve working space the ratio applies to the remaining room. Every request is still fitted to the hard budget, and mandatory input is judged only against it.
- Add **Settings → Models & API keys → add/edit → Advanced → Detect**: ask the provider for the model's context window (Anthropic model information, or `context_length`/`max_model_len`-style extras of an OpenAI-compatible `/models` list), fall back to an approximate built-in catalog for well-known families, fill the field and say which source answered. Detection never runs automatically, nothing is stored until you save, provider errors are reduced to fixed messages, and the defaults stay 32,768 / 4,096.
- Show a collapsed diagnostics block under each answer: estimated versus reported input tokens, cached tokens, tool rounds and elapsed time, with output tokens, compaction state, calibration ratio and per-phase timings inside. The numbers are stored with the message, sent once before the answer ends and excluded from tree exports. Both providers now report usage (OpenAI-compatible streams request the usage chunk, including DeepSeek cache hits, with a one-time fallback when a gateway rejects `stream_options`; Anthropic totals uncached, cache-creation and cache-read input tokens), and real answers with at least 2,000 estimated units teach a clamped per-model calibration ratio kept in the rebuildable `ModelCalibration` table and removed with the model. A ratio above 1 shrinks that model's input budget; ratios below 1 never enlarge it, and demo answers never learn.
- Replace the UTF-8 byte estimator with a character-class token estimator (about 0.7 unit per CJK character, 0.33 per visible ASCII character, 0.1 per whitespace character and 1 per other non-ASCII character, times a 1.15 safety factor), so Chinese conversations keep roughly three times as much original history before compaction and summaries start; tool results are trimmed by characters and re-verified. Summary, extraction and document producers keep their byte budgets with explicit conversion at the boundary. Estimates remain local approximations, not provider token counts.
- Add two switches under **Settings → Memory → Memory retrieval** to widen memory recall to other branches of the same topic and to other learning topics. The current learning path stays always on; widened facts are ranked with path facts but listed after them in their own labelled sections with a smaller budget, and every answer shows a compact **Referenced** line naming the memories it was given, each opening its source node.
- Search a topic's history from the learning map: besides instant title and quote matches, the search box lists full-text matches in completed answers, learning notes and facts with a verbatim snippet, and choosing an answer hit jumps to the passage. A SQLite FTS5 index maintained by database triggers backs the search and is rebuilt at start-up when needed; the answer model gets a matching read-only `search_learning_history` tool bound to the same scope switches.
- Store extracted topic facts with a type (definition, condition, correction, open question, learner state or conclusion) and key terms, shown as tags in **Settings → Memory**. A new fact can mark an earlier fact of the same learning path as superseded: it stays listed as **Superseded** until deleted but no longer reaches answers or search. Hand-edited facts are never superseded automatically.

- Add **Settings → Phone access**. Read-only Tailscale detection shows the one-time `tailscale serve` command, a QR code for the private HTTPS address, and a warning with `tailscale funnel reset` when Funnel makes the unauthenticated app public. The QR encoder is built in, adding no dependency.
- On touch screens, read text selections once they settle, since iOS long-press selection and handle drags send no pointer events. Dock selection actions at the bottom, away from the system menu and handles, and use 16px form text so iOS no longer zooms on focus.
- Keep answers generating when their page closes or its connection drops. Record every stream event so a page reconnects automatically and continues after the last event it received; Stop, node deletion and server shutdown still end an answer. Extract memories from completed answers even when no page is connected.
- Show changes from other windows and devices without reloading: a revisioned, ID-only change feed lets visible pages refresh the topic list, map, open conversation, models and MCP servers. Answers running elsewhere show their progress with a Stop control, and follow-ups on them wait until they finish.

- Keep user-written preferences separate from source-linked AI additions, with a learning switch, global/topic scope, editable additions and pending conflict suggestions. Prevent deleted preferences from replaying from the same question, protect manual edits, and remove derived additions with their sources. Unify the Settings frame across tabs with responsive internal scrolling.

- Keep the question composer empty until the user types, without contextual placeholder text.

- Dismiss top success notices automatically after ten seconds, restarting the timer for each new notice and retaining manual dismissal.

- Add accessible per-node delete menus to the learning map, including failed nodes and expanded follow-up rounds. Confirm the deletion scope, retain independent revisions, and refresh surviving source references. Unify the selected-text Copy, Explain and New branch button styles.

- Load large selected tool sets on demand within a schema budget, preserving exact argument definitions and restricting execution to tools advertised for that model round. Keep original-source readers available, report individual definitions that cannot fit, and finish bounded tool loops with a no-tool answer attempt. Preserve current inputs and stored originals while fitting later tool results.

- Add a compact question-navigation rail with hover previews, keyboard access and local scroll jumps, including legacy multi-turn nodes. Add selected-text copy beside inline Explain and New branch.

- Keep the preferences editor at a fixed height with internal scrolling.
- Make confirmed topic deletion permanent within the app: remove automatic deletion backups and the restore-last-deletion action, clear owned browser drafts and preserve unrelated topics and global settings.

- Add topic renaming, reversible archiving and an Archived view in Settings, with compact accessible action menus. Keep the sidebar focused on active topics. Preserve original questions, branches, memories and attachments; existing topics remain active after the additive database upgrade.
- Match the sidebar and browser icon to the native macOS application's mint-green rounded tree mark.

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
