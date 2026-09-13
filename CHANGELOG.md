# Changelog

## Unreleased

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
