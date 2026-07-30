import Foundation

@available(macOS 13.0, *)
private final class RecorderCommandController {
    private let recorder: Recorder
    private var signalSources: [DispatchSourceSignal] = []
    private var stdinSource: DispatchSourceRead?
    private var isStopping = false

    init(sessionURL: URL) {
        recorder = Recorder(
            sessionURL: sessionURL,
            showVUMeter: true
        ) { message in
            fputs("\(message)\n", stderr)
        }
    }

    func run() {
        let input = DispatchSource.makeReadSource(
            fileDescriptor: STDIN_FILENO,
            queue: .main
        )
        input.setEventHandler { [weak self] in
            input.cancel()
            self?.stopAndSave()
        }
        input.resume()
        stdinSource = input

        for signalNumber in [SIGINT, SIGTERM] {
            signal(signalNumber, SIG_IGN)
            let source = DispatchSource.makeSignalSource(
                signal: signalNumber,
                queue: .main
            )
            source.setEventHandler { [weak self, weak source] in
                source?.cancel()
                self?.stopAndSave()
            }
            source.resume()
            signalSources.append(source)
        }

        Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                try await recorder.start()
                fflush(stdout)
            } catch {
                recorder.markFailed(error)
                fputs(
                    "Failed to start: \(error.localizedDescription)\n",
                    stderr
                )
                fputs(
                    """

                    Grant recording permissions to the process used to start \
                    MeetRec in:
                    System Settings → Privacy & Security

                    """,
                    stderr
                )
                exit(1)
            }
        }

        RunLoop.main.run()
    }

    private func stopAndSave() {
        guard !isStopping else { return }
        isStopping = true
        Task { @MainActor in
            do {
                try await recorder.stop()
                exit(0)
            } catch {
                recorder.markFailed(error)
                fputs(
                    "Failed to stop recording: "
                        + "\(error.localizedDescription)\n",
                    stderr
                )
                exit(2)
            }
        }
    }
}

@main
private struct RecorderCommand {
    static func main() {
        guard #available(macOS 13.0, *) else {
            fputs("macOS 13.0 or later is required\n", stderr)
            exit(1)
        }

        if CommandLine.arguments.dropFirst().first == "--doctor" {
            runPermissionDoctor()
        }

        guard CommandLine.arguments.count >= 2 else {
            fputs("Usage: recorder <session-directory>\n", stderr)
            exit(1)
        }

        let sessionURL = URL(
            fileURLWithPath: CommandLine.arguments[1],
            isDirectory: true
        )
        let controller = RecorderCommandController(sessionURL: sessionURL)
        withExtendedLifetime(controller) {
            controller.run()
        }
    }
}
