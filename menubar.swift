import AppKit
import Foundation

private struct RecordingPreset: Sendable {
    let language: String
    let model: String
}

final class MenuController: NSObject, @unchecked Sendable {
    private let statusItem = NSStatusBar.system.statusItem(
        withLength: NSStatusItem.variableLength
    )
    private let projectURL: URL
    private let recordingsURL: URL
    private let recorderURL: URL
    private let pythonURL: URL

    private var recorderProcess: Process?
    private var recorderInput: Pipe?
    private var activeSessionURL: URL?
    private var activePreset: RecordingPreset?
    private var startedAt: Date?
    private var timer: Timer?

    init(projectURL: URL) {
        self.projectURL = projectURL
        self.recordingsURL = ProcessInfo.processInfo.environment[
            "RECORDINGS_DIR"
        ].map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? projectURL.appendingPathComponent("recordings", isDirectory: true)
        self.recorderURL = projectURL.appendingPathComponent(".recorder")
        self.pythonURL = projectURL.appendingPathComponent(".venv/bin/python")
        super.init()
        configureMenu()
        resumePendingTranscriptions()
    }

    private func configureMenu() {
        statusItem.button?.title = "✒︎"
        rebuildMenu()
    }

    private func rebuildMenu() {
        let menu = NSMenu()
        if recorderProcess == nil {
            menu.addItem(
                withTitle: "Start recording — Italiano",
                action: #selector(startItalian),
                keyEquivalent: "i"
            ).target = self
            menu.addItem(
                withTitle: "Start recording — English",
                action: #selector(startEnglish),
                keyEquivalent: "e"
            ).target = self
            menu.addItem(
                withTitle: "Start recording — Español",
                action: #selector(startSpanish),
                keyEquivalent: "s"
            ).target = self
            menu.addItem(
                withTitle: "Start recording — Auto detect",
                action: #selector(startAuto),
                keyEquivalent: "a"
            ).target = self
        } else {
            menu.addItem(
                withTitle: "Stop recording",
                action: #selector(stopRecording),
                keyEquivalent: "r"
            ).target = self
        }
        menu.addItem(.separator())
        menu.addItem(
            withTitle: "Open recordings",
            action: #selector(openRecordings),
            keyEquivalent: "o"
        ).target = self
        menu.addItem(
            withTitle: "Run doctor",
            action: #selector(runDoctor),
            keyEquivalent: "d"
        ).target = self
        menu.addItem(.separator())
        menu.addItem(
            withTitle: "Quit TapRecord",
            action: #selector(quit),
            keyEquivalent: "q"
        ).target = self
        statusItem.menu = menu
    }

    @objc private func startItalian() {
        startRecording(
            preset: RecordingPreset(language: "it", model: "medium")
        )
    }

    @objc private func startEnglish() {
        startRecording(
            preset: RecordingPreset(language: "en", model: "medium.en")
        )
    }

    @objc private func startSpanish() {
        startRecording(
            preset: RecordingPreset(language: "es", model: "medium")
        )
    }

    @objc private func startAuto() {
        startRecording(
            preset: RecordingPreset(language: "auto", model: "medium")
        )
    }

    private func startRecording(preset: RecordingPreset) {
        guard recorderProcess == nil else { return }
        guard FileManager.default.isExecutableFile(atPath: recorderURL.path) else {
            showAlert(
                title: "Recorder not built",
                message: "Run ./taprecord.sh build first."
            )
            return
        }
        guard FileManager.default.isExecutableFile(atPath: pythonURL.path) else {
            showAlert(
                title: "Python environment missing",
                message: "Create .venv and install the project requirements."
            )
            return
        }

        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd'T'HH-mm-ss"
        let sessionURL = recordingsURL.appendingPathComponent(
            formatter.string(from: Date()),
            isDirectory: true
        )
        do {
            try FileManager.default.createDirectory(
                at: sessionURL,
                withIntermediateDirectories: true
            )
            let logURL = sessionURL.appendingPathComponent("recorder.log")
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
            guard let log = FileHandle(forWritingAtPath: logURL.path) else {
                throw CocoaError(.fileWriteUnknown)
            }

            let input = Pipe()
            let process = Process()
            process.executableURL = recorderURL
            process.arguments = [sessionURL.path]
            process.currentDirectoryURL = projectURL
            process.standardInput = input
            process.standardOutput = log
            process.standardError = log
            process.terminationHandler = { [weak self] terminated in
                log.closeFile()
                DispatchQueue.main.async {
                    self?.recordingFinished(
                        sessionURL: sessionURL,
                        status: terminated.terminationStatus
                    )
                }
            }
            try process.run()
            recorderProcess = process
            recorderInput = input
            activeSessionURL = sessionURL
            activePreset = preset
            startedAt = Date()
            startTimer()
            rebuildMenu()
        } catch {
            showAlert(
                title: "Could not start recording",
                message: error.localizedDescription
            )
        }
    }

    @objc private func stopRecording() {
        guard let input = recorderInput else { return }
        try? input.fileHandleForWriting.write(
            contentsOf: Data("\n".utf8)
        )
        try? input.fileHandleForWriting.close()
        statusItem.button?.title = "…"
    }

    private func recordingFinished(sessionURL: URL, status: Int32) {
        let preset = activePreset
            ?? RecordingPreset(language: "it", model: "medium")
        stopTimer()
        recorderProcess = nil
        recorderInput = nil
        activeSessionURL = nil
        activePreset = nil
        startedAt = nil
        statusItem.button?.title = "✒︎"
        rebuildMenu()

        guard status == 0 else {
            showAlert(
                title: "Recording failed",
                message: "See \(sessionURL.path)/recorder.log"
            )
            return
        }
        enqueueAndTranscribe(sessionURL, preset: preset)
    }

    private func enqueueAndTranscribe(
        _ sessionURL: URL,
        preset: RecordingPreset
    ) {
        DispatchQueue.global(qos: .utility).async { [self] in
            let enqueue = runControl([
                "enqueue",
                sessionURL.path,
                "--language",
                preset.language,
                "--model",
                preset.model,
            ])
            guard enqueue == 0 else {
                notify(
                    title: "TapRecord",
                    message: "Could not enqueue \(sessionURL.lastPathComponent)"
                )
                return
            }
            let worker = runControl([
                "worker",
                "--once",
            ])
            notify(
                title: "TapRecord",
                message: worker == 0
                    ? "Transcript ready: \(sessionURL.lastPathComponent)"
                    : "Transcription needs attention: \(sessionURL.lastPathComponent)"
            )
        }
    }

    private func resumePendingTranscriptions() {
        DispatchQueue.global(qos: .utility).async { [self] in
            _ = runControl(["worker", "--once"])
        }
    }

    private func runControl(_ arguments: [String]) -> Int32 {
        let logURL = recordingsURL.appendingPathComponent(".menubar.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(
                atPath: logURL.path,
                contents: nil
            )
        }
        let log = FileHandle(forWritingAtPath: logURL.path)
        _ = try? log?.seekToEnd()
        let process = Process()
        process.executableURL = pythonURL
        process.arguments = ["-m", "meeting_recorder.control"] + arguments
        process.currentDirectoryURL = projectURL
        process.standardOutput = log
        process.standardError = log
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            try? log?.write(contentsOf: Data("\(error)\n".utf8))
            log?.closeFile()
            return 1
        }
        log?.closeFile()
        return process.terminationStatus
    }

    private func startTimer() {
        timer = Timer.scheduledTimer(
            withTimeInterval: 1,
            repeats: true
        ) { [weak self] _ in
            guard let self, let startedAt = self.startedAt else { return }
            let elapsed = Int(Date().timeIntervalSince(startedAt))
            self.statusItem.button?.title = String(
                format: "● %02d:%02d",
                elapsed / 60,
                elapsed % 60
            )
        }
    }

    private func stopTimer() {
        timer?.invalidate()
        timer = nil
    }

    @objc private func openRecordings() {
        try? FileManager.default.createDirectory(
            at: recordingsURL,
            withIntermediateDirectories: true
        )
        NSWorkspace.shared.open(recordingsURL)
    }

    @objc private func runDoctor() {
        DispatchQueue.global(qos: .utility).async { [self] in
            let status = runControl(["doctor"])
            DispatchQueue.main.async {
                self.showAlert(
                    title: status == 0
                        ? "TapRecord doctor"
                        : "TapRecord needs attention",
                    message: "See \(self.recordingsURL.path)/.menubar.log"
                )
            }
        }
    }

    private func notify(title: String, message: String) {
        let script = "display notification \(shellQuoted(message)) with title \(shellQuoted(title))"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]
        try? process.run()
    }

    private func shellQuoted(_ value: String) -> String {
        "\"\(value.replacingOccurrences(of: "\"", with: "\\\""))\""
    }

    private func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.runModal()
    }

    @objc private func quit() {
        if recorderProcess != nil {
            showAlert(
                title: "Recording in progress",
                message: "Stop the recording before quitting."
            )
            return
        }
        NSApplication.shared.terminate(nil)
    }
}

let projectURL: URL
if CommandLine.arguments.count >= 2 {
    projectURL = URL(
        fileURLWithPath: CommandLine.arguments[1],
        isDirectory: true
    )
} else {
    projectURL = URL(fileURLWithPath: CommandLine.arguments[0])
        .deletingLastPathComponent()
}

let application = NSApplication.shared
application.setActivationPolicy(.accessory)
let controller = MenuController(projectURL: projectURL)
withExtendedLifetime(controller) {
    application.run()
}
