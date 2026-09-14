# Contributing

Issues and pull requests in English or Chinese are welcome. For a substantial feature, describe the learning problem and proposed interaction in an issue first.

## Work locally

1. Fork and clone the repository. Install uv and Node.js 24.
2. Run `uv run python scripts/manage.py setup`. This basic installation skips semantic dependencies and model downloads.
3. Run `uv run python scripts/manage.py dev`. This uses `.runtime/dev.db` and initially seeds an offline demo model. Any models you add there are retained; tests use separate mock fixtures.
4. Make a focused change, preserving data compatibility and user content.
5. Run `uv run python scripts/manage.py check`. For chat, settings, navigation or map changes, also run `uv run python scripts/manage.py e2e` after installing its browser as described in the README.

Python is formatted with Ruff: `uv run ruff format app tests scripts examples integrations/mcp`. TypeScript follows the surrounding code. Commit lockfile changes with dependency changes. Add tests for behavior that could regress, using synthetic fixtures and mock providers.

Most changes need no local retrieval models. To work on real embedding/reranking behavior, run `uv run python scripts/manage.py retrieval`, then `uv run python scripts/prepare_retrieval.py --smoke` with hybrid mode enabled. The extra requires approximately 2.55 GiB of weights plus dependencies. Keep model-quality observations separate from mock regression results; see [the validation record](docs/memory-retrieval-validation.md).

## Product conventions

- A branch inherits its ancestor context and selected source passage. Siblings do not silently contribute facts.
- Editing creates a revision; retries retain previous partial output. Preserve history.
- Keep source anchors, drafts and reading positions across navigation and language changes.
- Keep the interface concise. Add Chinese and English labels together via the i18n helper; do not translate user content.
- Tests use an isolated database and do not contact a personal app or real model.

## Repository language

- Use descriptive ASCII filenames and paths, following the surrounding naming style. Use English for ordinary code comments, developer docstrings and the primary documentation.
- Keep `README.zh-CN.md`, localized documentation, bilingual interface strings and multilingual test fixtures. Never translate saved user content.
- Treat model prompts, tool descriptions and docstrings exposed through MCP as behavior-bearing text. Change them separately from comment cleanup and verify the affected tool schemas and behavior.
- Keep private files in `learning-materials/` or the existing legacy library out of commits. The release check enforces ASCII source paths and rejects private data even when force-staged.

## Pull requests

Explain the user-visible behavior and how you verified it. Screenshots must use synthetic content. Never commit databases, `.env`, keys, private learning material or browser profiles. After staging, run `uv run python scripts/check_release.py` to check the proposed source snapshot. Follow [SECURITY.md](SECURITY.md) for sensitive reports.

Record user-visible changes under `Unreleased` in `CHANGELOG.md`. A tagged release should include upgrade instructions and any new dependency, download or data-compatibility implications in `docs/releases/`.

Contributions are licensed under the project's MIT license.
