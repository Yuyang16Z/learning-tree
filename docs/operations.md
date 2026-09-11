# Local operation

Run commands from the repository root with uv and Node.js 24 installed.

| Command | Purpose |
| --- | --- |
| `uv run python scripts/manage.py setup` | Install Python and frontend dependencies from lockfiles. |
| `uv run python scripts/manage.py start --open` | Back up data, build and start on `127.0.0.1:8099`; open an existing app if already running. |
| `uv run python scripts/manage.py dev` | Isolated development on 8100/5174 with `.runtime/dev.db`; initially seeds a demo model and retains later user configurations. |
| `uv run python scripts/manage.py backup` | Online SQLite backup to `.backups/`. |
| `uv run python scripts/manage.py check` | Lint, format, unit/regression tests and frontend build. |
| `uv run python scripts/manage.py e2e` | UI checks with a temporary database and random local port. |

An occupied port is not forcibly freed. Ctrl+C stops the app. Use one backend per database; the standard launcher guards its port, but cannot police custom commands on other ports.

## Data locations

| Location | Contains |
| --- | --- |
| `branch_learning.db` | Normal chats, model keys, notes, memory and tool configuration. |
| `.runtime/dev.db` | Separate development data and demo configuration. |
| `.backups/` | Private database copies including credentials. |
| `学习资料/` | Files exposed to the optional file tool. |
| `mcp-data/` | MCP knowledge graph and browser artifacts. |
| Browser storage | Drafts, positions, delete-recovery snapshot and language. |

These local data files are excluded from Git. `DATABASE_URL` may select another SQLite file. Supplied commands resolve relative paths from the project root.

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
- **MCP path errors:** install dependencies and re-register from the current checkout. See the [MCP guide](../integrations/mcp/README.md).
- **Missing test browser:** run `node web/node_modules/playwright/cli.js install chromium --only-shell`; on Linux add `--with-deps` if system libraries are missing.
- **Windows:** use the terminal commands with uv/Node on PATH; `.command` is macOS-only. Browser and process behavior should be verified on your Windows environment; automated browser checks run on Linux.
