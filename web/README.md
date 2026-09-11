# LearningTree frontend

React, TypeScript and Vite. Follow the [root README](../README.md) for setup and commands.

`npm run dev` serves on loopback port 5174 and proxies `/api` to the development backend on port 8100. The frontend always uses same-origin `/api`, so an old environment file cannot redirect tests into a personal app. `npm run build` type-checks and creates `dist`, served by FastAPI in normal operation.

`App` coordinates `Sidebar`, `ChatPane` and `LearningMap`. Reusable layout, draft, stream and request helpers live in `src/lib`; localization lives in `src/i18n`. Use `useI18n().t(chinese, english)` for interface text. User-authored content stays unchanged.

Run `npm test` for unit tests. Use the root `manage.py e2e` command for browser checks against an isolated temporary backend, never a personal server.
