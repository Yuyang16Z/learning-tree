# LearningTree for macOS

A small native macOS application opens LearningTree in its own window, with a
Dock icon and standard window controls. It uses the existing local project and
Python environment, so your conversations, model settings, and documents remain
in the same place.

This is a desktop wrapper for a local installation. The generated app does not
bundle Python, project source, or your database and is not a standalone download
for another computer. Keep the project folder after installing the app.

## Build and install

Requirements:

- macOS 14 or later, Apple Silicon or Intel.
- Apple Command Line Tools (`xcode-select --install`).
- A working LearningTree checkout, including its `.venv` and web dependencies;
  follow the [project setup instructions](../../README.md) first.

From the project root:

```sh
uv run --inexact python desktop/macos/build.py --install
```

The builder compiles the web interface and native Swift application, draws the
application icon, and applies a local ad hoc signature. It does not install or
download missing dependencies.

Output:

- Build: `build/macos/LearningTree.app`
- Installed app: `/Applications/LearningTree.app`

Open **LearningTree / 学习树** from **Applications** or search for it with
Spotlight. You can keep its icon in the Dock using the Dock's Options menu.
Installation does not create a Desktop shortcut. No terminal window is needed
for normal use.

The app reuses a healthy LearningTree server on `127.0.0.1:8099`. Otherwise it
backs up the configured database and starts the existing Python environment.
Closing the window keeps the app running; clicking its Dock icon reopens it.
**Cmd+Q** quits and asks an app-started server to shut down gracefully. A server
started outside the app is left running. Long requests may finish in the
background before an app-started server stops.

File uploads and exports use native Open/Save dialogs. **Cmd+C/V/A/Z** provide
standard editing, and **Cmd+R** reconnects/reloads. External web links open in
your default browser. The **显示** menu opens the browser, project folder or
startup logs (`~/Library/Logs/LearningTree/server.log`).

Chats, model configurations and documents share the existing backend database.
Desktop drafts, reading positions, theme and language use persistent WebKit
storage, separate from Chrome/Safari/browser storage. Choose 中文/English in
the existing Settings panel; the macOS shell menus currently use Chinese.

To build without installing, omit `--install`. To reuse an already built web
interface, add `--skip-web-build`. You can specify an existing project location
with `--project-dir "/path/to/LearningTree"` and an existing Python environment
with `--python "/path/to/environment/bin/python"`; paths with spaces are supported.
Use `--applications-dir "$HOME/Applications"` if you prefer a per-user app folder
or do not have write access to `/Applications`. To explicitly add a Desktop
shortcut, include `--desktop-shortcut` alongside `--install`.

Only an existing application with the same LearningTree bundle identifier can
be replaced. If a Desktop shortcut was requested, conflicting files or shortcuts
are preserved and the installer reports the conflict. If you move the project,
rebuild and reinstall the app from its new location.

## Application configuration

The builder records the project location and Python executable in the generated
`Contents/Info.plist`, using `LearningTreeProjectPath` and
`LearningTreePythonPath`. These machine-specific paths exist only in ignored
build outputs and the installed application. The bundle identifier is
`app.learningtree.desktop`. The icon is drawn from code in `Icon.swift`.

The native window permits local networking without a global App Transport
Security exemption. The app is signed locally, not notarized for distribution.
Building on another Mac creates an app for that Mac's CPU architecture and local
project installation.

## Uninstall

Quit LearningTree and remove `/Applications/LearningTree.app` (or the custom
installation location you chose). Remove the Desktop shortcut only if you
explicitly created one. Your project and learning data remain in the project folder.
