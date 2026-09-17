import AppKit
import WebKit

// This native shell shares the checkout's backend and database. It never copies keys
// into its bundle, installs dependencies, or stops a server started by another app.
final class LearningTreeApp: NSObject, NSApplicationDelegate, NSWindowDelegate,
    WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var overlay: NSView!
    private var status: NSTextField!
    private var spinner: NSProgressIndicator!
    private var retry: NSButton!
    private var backend: Process?
    private var backup: Process?
    private var logHandle: FileHandle?
    private var pollTimer: Timer?
    private var startupID = UUID()
    private var quitting = false
    private var ready = false
    private var downloads: [ObjectIdentifier: (temporary: URL, destination: URL)] = [:]

    private let project = URL(fileURLWithPath:
        Bundle.main.object(forInfoDictionaryKey: "LearningTreeProjectPath") as? String ?? "")
    private var python: URL {
        URL(fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "LearningTreePythonPath")
            as? String ?? project.appendingPathComponent(".venv/bin/python").path)
    }
    private let port = Bundle.main.object(forInfoDictionaryKey: "LearningTreePort") as? Int ?? 8099
    private var origin: URL { URL(string: "http://127.0.0.1:\(port)")! }
    private let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/LearningTree/server.log")

    func applicationDidFinishLaunching(_ notification: Notification) {
        makeMenus()
        makeWindow()
        start()
        NSApp.activate(ignoringOtherApps: true)
    }

    private func makeMenus() {
        let bar = NSMenu()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于学习树", action: #selector(about), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏学习树", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出学习树", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let edit = NSMenu(title: "编辑")
        for (title, action, key) in [
            ("撤销", "undo:", "z"), ("剪切", "cut:", "x"),
            ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")
        ] {
            edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        let redo = edit.insertItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "z", at: 1)
        redo.keyEquivalentModifierMask = [.command, .shift]
        let view = NSMenu(title: "显示")
        view.addItem(withTitle: "重新加载", action: #selector(reload), keyEquivalent: "r")
        view.addItem(withTitle: "在浏览器中打开", action: #selector(openBrowser), keyEquivalent: "")
        view.addItem(.separator())
        view.addItem(withTitle: "项目文件夹", action: #selector(openProject), keyEquivalent: "")
        view.addItem(withTitle: "启动日志", action: #selector(openLogs), keyEquivalent: "")
        let windows = NSMenu(title: "窗口")
        windows.addItem(withTitle: "最小化", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        windows.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        // WKWebView also responds to reload; route app actions directly so Cmd+R
        // can recover a stopped backend before navigating the page.
        appMenu.items.first?.target = self
        for item in view.items where !item.isSeparatorItem { item.target = self }
        for menu in [appMenu, edit, view, windows] {
            let item = NSMenuItem(title: menu.title, action: nil, keyEquivalent: "")
            item.submenu = menu
            bar.addItem(item)
        }
        NSApp.mainMenu = bar
        NSApp.windowsMenu = windows
    }

    private func makeWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1380, height: 880),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "LearningTree · 学习树"
        window.minSize = NSSize(width: 960, height: 640)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        window.setFrameAutosaveName("LearningTreeWindow")

        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.translatesAutoresizingMaskIntoConstraints = false
        let content = window.contentView!
        content.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            webView.topAnchor.constraint(equalTo: content.topAnchor),
            webView.bottomAnchor.constraint(equalTo: content.bottomAnchor)
        ])

        overlay = NSView()
        overlay.wantsLayer = true
        overlay.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        overlay.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(overlay)
        NSLayoutConstraint.activate([
            overlay.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            overlay.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            overlay.topAnchor.constraint(equalTo: content.topAnchor),
            overlay.bottomAnchor.constraint(equalTo: content.bottomAnchor)
        ])
        let title = NSTextField(labelWithString: "学习树")
        title.font = NSFont.systemFont(ofSize: 28, weight: .semibold)
        spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .regular
        status = NSTextField(wrappingLabelWithString: "正在打开你的学习空间…")
        status.alignment = .center
        status.textColor = .secondaryLabelColor
        status.preferredMaxLayoutWidth = 540
        retry = NSButton(title: "重试", target: self, action: #selector(start))
        retry.bezelStyle = .rounded
        let folder = NSButton(title: "项目文件夹", target: self, action: #selector(openProject))
        folder.bezelStyle = .rounded
        let buttons = NSStackView(views: [retry, folder])
        buttons.spacing = 10
        let stack = NSStackView(views: [title, spinner, status, buttons])
        stack.orientation = .vertical
        stack.spacing = 20
        stack.translatesAutoresizingMaskIntoConstraints = false
        overlay.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: overlay.centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualToConstant: 560)
        ])
        window.makeKeyAndOrderFront(nil)
    }

    private enum Health { case ready, unavailable, otherService }

    private func checkHealth(_ completion: @escaping (Health) -> Void) {
        var request = URLRequest(url: origin.appendingPathComponent("health"))
        request.timeoutInterval = 2
        request.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: request) { data, response, error in
            let result: Health
            if let response = response as? HTTPURLResponse {
                let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: String]
                result = response.statusCode == 200 && json?["status"] == "ok"
                    && json?["name"] == "学习树" ? .ready : .otherService
            } else {
                // A timeout can be an occupied port; uvicorn will safely refuse to bind it.
                result = .unavailable
            }
            DispatchQueue.main.async { completion(result) }
        }.resume()
    }

    @objc private func start() {
        guard !quitting, backup == nil else { return }
        startupID = UUID()
        let id = startupID
        ready = false
        pollTimer?.invalidate()
        overlay.isHidden = false
        status.stringValue = "正在打开你的学习空间…"
        spinner.startAnimation(nil)
        retry.isHidden = true
        checkHealth { [weak self] health in
            guard let self, self.startupID == id, !self.quitting else { return }
            switch health {
            case .ready: self.loadWorkspace()
            case .otherService: self.fail("端口 \(self.port) 已被其他服务占用。请关闭冲突的服务后重试。")
            case .unavailable:
                if self.backend?.isRunning == true { self.waitForServer(id: id) }
                else { self.prepareServer(id: id) }
            }
        }
    }

    private func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        env["PATH"] = [project.appendingPathComponent(".venv/bin").path,
            "\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin",
            env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"].joined(separator: ":")
        env["PYTHONUNBUFFERED"] = "1"
        return env
    }

    private func process(arguments: [String]) -> Process {
        let child = Process()
        child.executableURL = python
        child.arguments = arguments
        child.currentDirectoryURL = project
        child.environment = environment()
        child.standardInput = FileHandle.nullDevice
        child.standardOutput = logHandle ?? FileHandle.nullDevice
        child.standardError = logHandle ?? FileHandle.nullDevice
        return child
    }

    private func prepareServer(id: UUID) {
        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: python.path),
              fm.fileExists(atPath: project.appendingPathComponent("app/main.py").path),
              fm.fileExists(atPath: project.appendingPathComponent("web/dist/index.html").path) else {
            fail("找不到完整的学习树项目。请保留原项目文件夹；移动项目后，从新位置重新安装桌面应用。")
            return
        }
        do {
            try fm.createDirectory(at: logURL.deletingLastPathComponent(),
                withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            if !fm.fileExists(atPath: logURL.path) {
                fm.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
            }
            logHandle = try FileHandle(forWritingTo: logURL)
            try logHandle?.truncate(atOffset: 0)
            let child = process(arguments: ["scripts/backup_data.py"])
            backup = child
            status.stringValue = "正在备份记录并启动…"
            child.terminationHandler = { [weak self] child in
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.backup = nil
                    guard self.startupID == id, !self.quitting else { return }
                    guard child.terminationStatus == 0 else {
                        self.fail("记录备份未完成，尚未启动服务。可在「显示 → 启动日志」查看原因后重试。")
                        return
                    }
                    // Another launcher may have started the service during the backup.
                    self.checkHealth { health in
                        guard self.startupID == id, !self.quitting else { return }
                        if health == .ready { self.loadWorkspace() }
                        else if health == .otherService { self.fail("端口 \(self.port) 已被其他服务占用。") }
                        else { self.launchServer(id: id) }
                    }
                }
            }
            try child.run()
            DispatchQueue.main.asyncAfter(deadline: .now() + 60) { [weak self, weak child] in
                guard let self, self.startupID == id, child?.isRunning == true else { return }
                child?.terminate()
            }
        } catch {
            backup = nil
            fail("无法启动本地服务：\(error.localizedDescription)")
        }
    }

    private func launchServer(id: UUID) {
        let child = process(arguments: ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)])
        backend = child
        child.terminationHandler = { [weak self] child in
            DispatchQueue.main.async {
                guard let self, self.backend === child, !self.quitting else { return }
                self.backend = nil
                self.checkHealth { health in
                    guard self.startupID == id, !self.quitting else { return }
                    if health == .ready { self.loadWorkspace() }
                    else { self.fail("本地服务已停止。点击「重试」重新启动；详情见「显示 → 启动日志」。") }
                }
            }
        }
        do {
            try child.run()
            waitForServer(id: id)
        } catch {
            backend = nil
            fail("无法运行本地服务：\(error.localizedDescription)")
        }
    }

    private func waitForServer(id: UUID) {
        let deadline = Date().addingTimeInterval(60)
        func poll() {
            checkHealth { [weak self] health in
                guard let self, self.startupID == id, !self.quitting, !self.ready else { return }
                if health == .ready { self.loadWorkspace() }
                else if health == .otherService { self.fail("端口 \(self.port) 已被其他服务占用。") }
                else if Date() >= deadline {
                    self.fail("启动时间较长。稍后点击「重试」，或在「显示 → 启动日志」查看状态。")
                } else {
                    self.pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: false) { _ in poll() }
                }
            }
        }
        poll()
    }

    private func loadWorkspace() {
        guard !ready else { return }
        ready = true
        pollTimer?.invalidate()
        status.stringValue = "正在载入…"
        webView.load(URLRequest(url: origin))
    }

    private func fail(_ message: String) {
        pollTimer?.invalidate()
        overlay.isHidden = false
        status.stringValue = message
        spinner.stopAnimation(nil)
        retry.isHidden = false
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !quitting else { return .terminateLater }
        quitting = true
        pollTimer?.invalidate()
        backup?.terminate()
        guard let child = backend, child.isRunning else { return .terminateNow }
        child.terminate()
        // Give uvicorn time to finish requests and close MCP sessions. If a long
        // request is still running, allow graceful draining beyond the app's exit.
        DispatchQueue.global().async {
            let deadline = Date().addingTimeInterval(4)
            while child.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
            DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    @objc private func about() { NSApp.orderFrontStandardAboutPanel(nil) }
    @objc private func reload() { start() }
    @objc private func openBrowser() { NSWorkspace.shared.open(origin) }
    @objc private func openProject() { NSWorkspace.shared.open(project) }
    @objc private func openLogs() { NSWorkspace.shared.open(logURL.deletingLastPathComponent()) }

    private func isLocal(_ url: URL?) -> Bool {
        guard let url else { return false }
        return url.scheme == "http" && url.host == "127.0.0.1" && url.port == port
    }

    private func isLocalBlob(_ url: URL?) -> Bool {
        guard let url, url.scheme == "blob" else { return false }
        return isLocal(URL(string: String(url.absoluteString.dropFirst(5))))
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if isLocal(action.request.url) || isLocalBlob(action.request.url) {
            decisionHandler(action.shouldPerformDownload ? .download : .allow)
        } else {
            if action.navigationType == .linkActivated, let url = action.request.url,
               ["https", "http", "mailto"].contains(url.scheme ?? "") {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if action.targetFrame == nil, isLocal(action.request.url) { webView.load(action.request) }
        return nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disposition = (response.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        decisionHandler(!response.canShowMIMEType || disposition.lowercased().hasPrefix("attachment") ? .download : .allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        spinner.stopAnimation(nil)
        overlay.isHidden = true
        window.makeFirstResponder(webView)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let failure = error as NSError
        // A navigation handed to a download is intentionally interrupted.
        if failure.domain == "WebKitErrorDomain" && failure.code == 102 { return }
        if failure.domain == NSURLErrorDomain && failure.code == NSURLErrorCancelled { return }
        fail("页面载入失败，请重试。\n\(error.localizedDescription)")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { start() }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        guard isLocal(frame.request.url) else { completionHandler(nil); return }
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.canChooseFiles = true
        panel.beginSheetModal(for: window) { result in completionHandler(result == .OK ? panel.urls : nil) }
    }

    private func alert(_ message: String) -> NSAlert {
        let dialog = NSAlert()
        dialog.messageText = "LearningTree"
        dialog.informativeText = message
        dialog.addButton(withTitle: "确定")
        return dialog
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        guard isLocal(frame.request.url) else { completionHandler(); return }
        alert(message).beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        guard isLocal(frame.request.url) else { completionHandler(false); return }
        let dialog = alert(message)
        dialog.addButton(withTitle: "取消")
        dialog.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        guard isLocal(frame.request.url) else { completionHandler(nil); return }
        let dialog = alert(prompt)
        dialog.addButton(withTitle: "取消")
        let input = NSTextField(string: defaultText ?? "")
        input.frame = NSRect(x: 0, y: 0, width: 320, height: 24)
        dialog.accessoryView = input
        dialog.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn ? input.stringValue : nil) }
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        let filename = (suggestedFilename as NSString).lastPathComponent
        panel.nameFieldStringValue = filename.isEmpty || filename == "." || filename == ".." ? "LearningTree-export.json" : filename
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { [weak self] result in
            guard let self, result == .OK, let destination = panel.url else { completionHandler(nil); return }
            let temporary = destination.deletingLastPathComponent()
                .appendingPathComponent(".learningtree-\(UUID().uuidString).part")
            self.downloads[ObjectIdentifier(download)] = (temporary, destination)
            completionHandler(temporary)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let files = downloads.removeValue(forKey: ObjectIdentifier(download)) else { return }
        do {
            if FileManager.default.fileExists(atPath: files.destination.path) {
                _ = try FileManager.default.replaceItemAt(files.destination, withItemAt: files.temporary)
            } else {
                try FileManager.default.moveItem(at: files.temporary, to: files.destination)
            }
        } catch {
            try? FileManager.default.removeItem(at: files.temporary)
            alert("保存失败：\(error.localizedDescription)").beginSheetModal(for: window)
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if let files = downloads.removeValue(forKey: ObjectIdentifier(download)) {
            try? FileManager.default.removeItem(at: files.temporary)
        }
        if (error as NSError).code != NSURLErrorCancelled {
            alert("下载失败：\(error.localizedDescription)").beginSheetModal(for: window)
        }
    }
}

let app = NSApplication.shared
let delegate = LearningTreeApp()
app.setActivationPolicy(.regular)
app.delegate = delegate
app.run()
