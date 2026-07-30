import AppKit
import Foundation

private struct RecordingPreset: Sendable {
    let language: String
    let model: String
}

private struct JobProgress: Decodable {
    let phase: String
    let fraction: Double
    let detail: String
    let indeterminate: Bool
}

private struct JobStatus: Decodable {
    let state: String
    let progress: JobProgress?
}

private final class SessionLog: @unchecked Sendable {
    private let handle: FileHandle
    private let lock = NSLock()

    init(url: URL) throws {
        FileManager.default.createFile(atPath: url.path, contents: nil)
        guard let handle = FileHandle(forWritingAtPath: url.path) else {
            throw CocoaError(.fileWriteUnknown)
        }
        self.handle = handle
    }

    func write(_ message: String) {
        lock.lock()
        defer { lock.unlock() }
        try? handle.write(contentsOf: Data("\(message)\n".utf8))
    }

    func close() {
        lock.lock()
        defer { lock.unlock() }
        try? handle.close()
    }
}

final class MenuController: NSObject, @unchecked Sendable {
    private let statusItem = NSStatusBar.system.statusItem(
        withLength: NSStatusItem.variableLength
    )
    private let projectURL: URL
    private let recordingsURL: URL
    private let pythonURL: URL
    /// Recovery at launch and post-recording transcription both run
    /// `worker --once`. A serial queue keeps them from overlapping: the
    /// second run starts after the first finishes and picks up whatever it
    /// enqueued in the meantime.
    private let workerQueue = DispatchQueue(
        label: "meetrec.transcription-worker",
        qos: .utility
    )

    private var activeRecorder: Recorder?
    private var activeLog: SessionLog?
    private var activeSessionURL: URL?
    private var activePreset: RecordingPreset?
    private var startedAt: Date?
    private var timer: Timer?
    private var processingSessionURL: URL?
    private var processingTimer: Timer?
    private var currentProgress: JobProgress?
    private var progressLabel: NSTextField?
    private var progressIndicator: NSProgressIndicator?

    init(projectURL: URL) {
        self.projectURL = projectURL
        self.recordingsURL = ProcessInfo.processInfo.environment[
            "RECORDINGS_DIR"
        ].map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? projectURL.appendingPathComponent("recordings", isDirectory: true)
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
        if processingSessionURL != nil {
            addProgressItem(to: menu)
            menu.addItem(.separator())
        }
        if activeRecorder == nil {
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
            withTitle: "Quit MeetRec",
            action: #selector(quit),
            keyEquivalent: "q"
        ).target = self
        statusItem.menu = menu
    }

    private func addProgressItem(to menu: NSMenu) {
        let item = NSMenuItem()
        let view = NSView(frame: NSRect(x: 0, y: 0, width: 320, height: 58))
        let label = NSTextField(labelWithString: "Preparing transcript…")
        label.frame = NSRect(x: 14, y: 32, width: 292, height: 18)
        label.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        label.lineBreakMode = .byTruncatingTail

        let indicator = NSProgressIndicator(
            frame: NSRect(x: 14, y: 12, width: 292, height: 12)
        )
        indicator.style = .bar
        indicator.controlSize = .small
        indicator.minValue = 0
        indicator.maxValue = 1

        view.addSubview(label)
        view.addSubview(indicator)
        item.view = view
        menu.addItem(item)
        progressLabel = label
        progressIndicator = indicator
        updateProgressUI()
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
        guard activeRecorder == nil else { return }
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
            let sessionLog = try SessionLog(url: logURL)
            let recorder = Recorder(
                sessionURL: sessionURL,
                showVUMeter: false
            ) { message in
                sessionLog.write(message)
            }

            activeRecorder = recorder
            activeLog = sessionLog
            activeSessionURL = sessionURL
            activePreset = preset
            startedAt = Date()
            startTimer()
            rebuildMenu()

            Task { @MainActor [weak self, recorder] in
                do {
                    try await recorder.start()
                } catch {
                    recorder.markFailed(error)
                    self?.recordingFinished(
                        sessionURL: sessionURL,
                        status: 1
                    )
                }
            }
        } catch {
            showAlert(
                title: "Could not start recording",
                message: error.localizedDescription
            )
        }
    }

    @objc private func stopRecording() {
        guard let recorder = activeRecorder,
              let sessionURL = activeSessionURL
        else {
            return
        }
        statusItem.button?.title = "…"
        Task { @MainActor [weak self, recorder] in
            do {
                try await recorder.stop()
                self?.recordingFinished(
                    sessionURL: sessionURL,
                    status: 0
                )
            } catch {
                recorder.markFailed(error)
                self?.recordingFinished(
                    sessionURL: sessionURL,
                    status: 2
                )
            }
        }
    }

    private func recordingFinished(sessionURL: URL, status: Int32) {
        let preset = activePreset
            ?? RecordingPreset(language: "it", model: "medium")
        stopTimer()
        activeLog?.close()
        activeRecorder = nil
        activeLog = nil
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
        workerQueue.async { [self] in
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
                    title: "MeetRec",
                    message: "Could not enqueue \(sessionURL.lastPathComponent)"
                )
                return
            }
            DispatchQueue.main.async {
                self.startProgressMonitoring(sessionURL)
            }
            let worker = runControl([
                "worker",
                "--once",
            ])
            DispatchQueue.main.async {
                self.pollProgress()
            }
            notify(
                title: "MeetRec",
                message: worker == 0
                    ? "Transcript ready: \(sessionURL.lastPathComponent)"
                    : "Transcription needs attention: \(sessionURL.lastPathComponent)"
            )
        }
    }

    private func resumePendingTranscriptions() {
        workerQueue.async { [self] in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self.startProgressMonitoring(nil)
            }
            _ = runControl(["worker", "--once"])
            DispatchQueue.main.async {
                self.pollProgress()
            }
        }
    }

    private func startProgressMonitoring(_ sessionURL: URL?) {
        let resolved = sessionURL ?? findActiveSession()
        guard let resolved else { return }
        processingSessionURL = resolved
        currentProgress = nil
        processingTimer?.invalidate()
        processingTimer = Timer.scheduledTimer(
            withTimeInterval: 0.5,
            repeats: true
        ) { [weak self] _ in
            self?.pollProgress()
        }
        rebuildMenu()
        pollProgress()
    }

    private func pollProgress() {
        guard let sessionURL = processingSessionURL else {
            if let active = findActiveSession() {
                startProgressMonitoring(active)
            }
            return
        }
        let jobURL = sessionURL.appendingPathComponent("job.json")
        guard
            let data = try? Data(contentsOf: jobURL),
            let job = try? JSONDecoder().decode(JobStatus.self, from: data)
        else {
            return
        }

        currentProgress = job.progress
        updateProgressUI()

        if job.state == "complete" {
            if let next = findActiveSession() {
                startProgressMonitoring(next)
            } else {
                finishProgress(success: true)
            }
        } else if job.state == "failed" {
            finishProgress(success: false)
        }
    }

    private func findActiveSession() -> URL? {
        guard
            let entries = try? FileManager.default.contentsOfDirectory(
                at: recordingsURL,
                includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles]
            )
        else {
            return nil
        }

        var pending: [URL] = []
        var processing: [URL] = []
        for entry in entries {
            let jobURL = entry.appendingPathComponent("job.json")
            guard
                let data = try? Data(contentsOf: jobURL),
                let job = try? JSONDecoder().decode(
                    JobStatus.self,
                    from: data
                )
            else {
                continue
            }
            if job.state == "processing" {
                processing.append(entry)
            } else if job.state == "pending" {
                pending.append(entry)
            }
        }
        return (processing.sorted { $0.path < $1.path }
            + pending.sorted { $0.path < $1.path }).first
    }

    private func updateProgressUI() {
        guard let progress = currentProgress else {
            progressLabel?.stringValue = "Preparing transcript…"
            progressIndicator?.isIndeterminate = true
            progressIndicator?.startAnimation(nil)
            if activeRecorder == nil {
                statusItem.button?.title = "✒︎ …"
            }
            return
        }

        let title = phaseTitle(progress.phase)
        if progress.indeterminate {
            progressLabel?.stringValue = "\(title)…"
            progressIndicator?.isIndeterminate = true
            progressIndicator?.startAnimation(nil)
            if activeRecorder == nil {
                statusItem.button?.title = "✒︎ …"
            }
        } else {
            let percent = Int((progress.fraction * 100).rounded())
            progressLabel?.stringValue = "\(title) — \(percent)%"
            progressIndicator?.stopAnimation(nil)
            progressIndicator?.isIndeterminate = false
            progressIndicator?.doubleValue = progress.fraction
            if activeRecorder == nil {
                statusItem.button?.title = "✒︎ \(percent)%"
            }
        }
        progressLabel?.toolTip = progress.detail
    }

    private func phaseTitle(_ phase: String) -> String {
        switch phase {
        case "queued":
            return "In coda"
        case "starting":
            return "Avvio elaborazione"
        case "loading_model":
            return "Caricamento faster-whisper"
        case "transcribing_microphone":
            return "Trascrizione microfono"
        case "loading_diarizer":
            return "Caricamento pyannote"
        case "transcribing_system":
            return "Trascrizione audio di sistema"
        case "diarizing_system":
            return "Diarizzazione interlocutori"
        case "diarization_complete":
            return "Diarizzazione completata"
        case "merging":
            return "Unione delle timeline"
        case "writing_outputs":
            return "Scrittura transcript"
        case "on_stop":
            return "Operazione finale"
        case "complete":
            return "Transcript completato"
        case "retrying":
            return "Nuovo tentativo"
        case "failed":
            return "Elaborazione fallita"
        default:
            return "Elaborazione transcript"
        }
    }

    private func finishProgress(success: Bool) {
        processingTimer?.invalidate()
        processingTimer = nil
        processingSessionURL = nil
        currentProgress = nil
        progressLabel = nil
        progressIndicator = nil
        if activeRecorder == nil {
            statusItem.button?.title = success ? "✒︎ ✓" : "✒︎ !"
        }
        rebuildMenu()
        if success {
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                if self.activeRecorder == nil
                    && self.processingSessionURL == nil
                {
                    self.statusItem.button?.title = "✒︎"
                }
            }
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
                        ? "MeetRec doctor"
                        : "MeetRec needs attention",
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
        if activeRecorder != nil {
            showAlert(
                title: "Recording in progress",
                message: "Stop the recording before quitting."
            )
            return
        }
        NSApplication.shared.terminate(nil)
    }
}

/// Holds an exclusive lock for as long as the process lives. A file lock works
/// no matter how the app was started, which `NSRunningApplication` does not:
/// launchd runs the executable directly rather than through LaunchServices.
/// Two instances would put two icons in the menu bar and have two recorders
/// competing for the microphone.
private func acquireSingleInstanceLock() -> Bool {
    let supportURL = FileManager.default.urls(
        for: .applicationSupportDirectory,
        in: .userDomainMask
    ).first?.appendingPathComponent("MeetRec", isDirectory: true)
    guard let supportURL else { return true }
    try? FileManager.default.createDirectory(
        at: supportURL,
        withIntermediateDirectories: true
    )

    let lockURL = supportURL.appendingPathComponent("instance.lock")
    let descriptor = open(lockURL.path, O_CREAT | O_RDWR, 0o644)
    guard descriptor >= 0 else { return true }
    guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
        close(descriptor)
        return false
    }
    // The descriptor is deliberately never closed: the lock has to outlive
    // this function and is released by the kernel when the process exits.
    return true
}

@main
private struct MeetRecMenuApplication {
    static func main() {
        guard #available(macOS 13.0, *) else {
            fputs("macOS 13.0 or later is required\n", stderr)
            exit(1)
        }

        if CommandLine.arguments.dropFirst().first == "--doctor" {
            runPermissionDoctor()
        }

        guard acquireSingleInstanceLock() else {
            fputs("MeetRec is already running\n", stderr)
            exit(0)
        }

        let projectURL: URL
        if CommandLine.arguments.count >= 2 {
            projectURL = URL(
                fileURLWithPath: CommandLine.arguments[1],
                isDirectory: true
            )
        } else if Bundle.main.bundleURL.pathExtension == "app" {
            projectURL = Bundle.main.bundleURL.deletingLastPathComponent()
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
    }
}
