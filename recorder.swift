import AVFoundation
import CoreMedia
import CoreGraphics
import Foundation
import ScreenCaptureKit

// MARK: - Session metadata

private struct TrackOffsets: Encodable {
    var mic: Double?
    var system: Double?
}

private struct TrackFiles: Encodable {
    let mic = "audio/mic.caf"
    let system = "audio/system.caf"
}

private struct SessionMeta: Encodable {
    let schemaVersion = 1
    var state: String
    let startedAt: String
    var endedAt: String?
    var durationSeconds: Double?
    var trackStartOffsets: TrackOffsets
    let tracks = TrackFiles()
    var warnings: [String]
    var error: String?
}

private func iso8601(_ date: Date = Date()) -> String {
    ISO8601DateFormatter().string(from: date)
}

// MARK: - Recorder

@available(macOS 13.0, *)
final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private let sessionURL: URL
    private let audioURL: URL
    private let micURL: URL
    private let systemURL: URL
    private let metaURL: URL

    private var stream: SCStream?
    private var systemFile: AVAudioFile?
    private var audioEngine: AVAudioEngine?
    private var micFile: AVAudioFile?

    private var micLevel: Float = 0
    private var systemLevel: Float = 0
    private var vuTimer: DispatchSourceTimer?

    private let stateLock = NSLock()
    private var stopping = false
    private var startedUptime = ProcessInfo.processInfo.systemUptime
    private var metadata: SessionMeta

    init(sessionURL: URL) {
        self.sessionURL = sessionURL
        self.audioURL = sessionURL.appendingPathComponent("audio", isDirectory: true)
        self.micURL = audioURL.appendingPathComponent("mic.caf")
        self.systemURL = audioURL.appendingPathComponent("system.caf")
        self.metaURL = sessionURL.appendingPathComponent("meta.json")
        self.metadata = SessionMeta(
            state: "starting",
            startedAt: iso8601(),
            trackStartOffsets: TrackOffsets(),
            warnings: []
        )
        super.init()
    }

    // MARK: - Metadata

    private func writeMetadata() throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        try encoder.encode(metadata).write(to: metaURL, options: .atomic)
    }

    private func writeInitialMetadata() throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        try encoder.encode(metadata).write(to: metaURL, options: .atomic)
    }

    private func setTrackOffset(_ track: String) {
        stateLock.lock()
        defer { stateLock.unlock() }
        let offset = max(0, ProcessInfo.processInfo.systemUptime - startedUptime)
        if track == "mic", metadata.trackStartOffsets.mic == nil {
            metadata.trackStartOffsets.mic = offset
            try? writeMetadata()
        } else if track == "system", metadata.trackStartOffsets.system == nil {
            metadata.trackStartOffsets.system = offset
            try? writeMetadata()
        }
    }

    // MARK: - Start

    func start() async throws {
        try FileManager.default.createDirectory(
            at: audioURL,
            withIntermediateDirectories: true
        )
        startedUptime = ProcessInfo.processInfo.systemUptime
        metadata.state = "recording"
        try writeInitialMetadata()

        try await startSystemCapture()
        do {
            try startMicCapture()
        } catch {
            let warning = "Microphone unavailable; only system audio was recorded: \(error.localizedDescription)"
            metadata.warnings.append(warning)
            try? writeMetadata()
            fputs("Warning: \(warning)\n", stderr)
        }
        startVUMeter()
    }

    private func startSystemCapture() async throws {
        let content = try await SCShareableContent.current
        guard let display = content.displays.first else {
            throw RecorderError.noDisplay
        }

        let configuration = SCStreamConfiguration()
        configuration.capturesAudio = true
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(seconds: 1, preferredTimescale: 1)

        let filter = SCContentFilter(
            display: display,
            excludingApplications: [],
            exceptingWindows: []
        )
        stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try stream!.addStreamOutput(
            self,
            type: .audio,
            sampleHandlerQueue: DispatchQueue(label: "taprecord.system-audio")
        )
        try await stream!.startCapture()
    }

    private func startMicCapture() throws {
        audioEngine = AVAudioEngine()
        let inputNode = audioEngine!.inputNode
        let hardwareRate = inputNode.inputFormat(forBus: 0).sampleRate
        guard
            let format = AVAudioFormat(
                standardFormatWithSampleRate: hardwareRate,
                channels: 1
            )
        else {
            throw RecorderError.micFormatUnavailable
        }

        micFile = try AVAudioFile(forWriting: micURL, settings: format.settings)
        inputNode.installTap(onBus: 0, bufferSize: 4096, format: format) {
            [weak self] buffer, _ in
            guard let self, !self.stopping else { return }
            if self.metadata.trackStartOffsets.mic == nil {
                self.setTrackOffset("mic")
            }
            do {
                try self.micFile?.write(from: buffer)
            } catch {
                fputs("Microphone write error: \(error.localizedDescription)\n", stderr)
            }
            self.micLevel = self.rms(buffer: buffer)
        }
        try audioEngine!.start()
    }

    // MARK: - System audio conversion

    private func appendSystemAudio(_ sampleBuffer: CMSampleBuffer) {
        let frameCount = CMSampleBufferGetNumSamples(sampleBuffer)
        guard frameCount > 0 else { return }
        guard
            let description = CMSampleBufferGetFormatDescription(sampleBuffer),
            let streamDescription = CMAudioFormatDescriptionGetStreamBasicDescription(description),
            let format = AVAudioFormat(streamDescription: streamDescription),
            let pcmBuffer = AVAudioPCMBuffer(
                pcmFormat: format,
                frameCapacity: AVAudioFrameCount(frameCount)
            )
        else {
            fputs("Could not decode the system audio format\n", stderr)
            return
        }

        pcmBuffer.frameLength = AVAudioFrameCount(frameCount)
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer,
            at: 0,
            frameCount: Int32(frameCount),
            into: pcmBuffer.mutableAudioBufferList
        )
        guard status == noErr else {
            fputs("System audio conversion error: \(status)\n", stderr)
            return
        }

        do {
            if systemFile == nil {
                systemFile = try AVAudioFile(
                    forWriting: systemURL,
                    settings: format.settings
                )
                setTrackOffset("system")
            }
            try systemFile?.write(from: pcmBuffer)
            systemLevel = rms(buffer: pcmBuffer)
        } catch {
            fputs("System audio write error: \(error.localizedDescription)\n", stderr)
        }
    }

    // MARK: - VU meter

    private func startVUMeter() {
        print("")
        vuTimer = DispatchSource.makeTimerSource(queue: .main)
        vuTimer!.schedule(deadline: .now(), repeating: .milliseconds(150))
        vuTimer!.setEventHandler { [weak self] in
            guard let self else { return }
            print(
                "\r  🎤 \(vuBar(self.micLevel))  🔊 \(vuBar(self.systemLevel))  ",
                terminator: ""
            )
            fflush(stdout)
        }
        vuTimer!.resume()
    }

    private func stopVUMeter() {
        vuTimer?.cancel()
        vuTimer = nil
        print("")
    }

    // MARK: - Stop

    func stop() async throws {
        guard !stopping else { return }
        stopping = true
        stopVUMeter()

        audioEngine?.inputNode.removeTap(onBus: 0)
        audioEngine?.stop()
        micFile = nil

        try? await stream?.stopCapture()
        systemFile = nil

        metadata.state = "recorded"
        metadata.endedAt = iso8601()
        metadata.durationSeconds = max(
            0,
            ProcessInfo.processInfo.systemUptime - startedUptime
        )
        try writeMetadata()
    }

    func markFailed(_ error: Error) {
        metadata.state = "recording_failed"
        metadata.endedAt = iso8601()
        metadata.durationSeconds = max(
            0,
            ProcessInfo.processInfo.systemUptime - startedUptime
        )
        metadata.error = error.localizedDescription
        try? writeMetadata()
    }

    // MARK: - SCStreamOutput

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio, !stopping else { return }
        appendSystemAudio(sampleBuffer)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("Stream error: \(error.localizedDescription)\n", stderr)
    }

    // MARK: - Level helper

    private func rms(buffer: AVAudioPCMBuffer) -> Float {
        guard let data = buffer.floatChannelData?[0] else { return 0 }
        let count = Int(buffer.frameLength)
        guard count > 0 else { return 0 }
        var sum: Float = 0
        for index in 0 ..< count {
            sum += data[index] * data[index]
        }
        return sqrt(sum / Float(count))
    }
}

// MARK: - UI helpers

private func vuBar(_ rms: Float, width: Int = 12) -> String {
    let db = 20 * log10(max(rms, 1e-9))
    let level = min(max((db + 60) / 60, 0), 1)
    let filled = Int(level * Float(width))
    return String(repeating: "█", count: filled)
        + String(repeating: "░", count: width - filled)
}

enum RecorderError: Error, LocalizedError {
    case noDisplay
    case noAudioCaptured
    case micFormatUnavailable

    var errorDescription: String? {
        switch self {
        case .noDisplay:
            return "No display found"
        case .noAudioCaptured:
            return "No audio was captured"
        case .micFormatUnavailable:
            return "Could not create the microphone audio format"
        }
    }
}

// MARK: - Entry point

guard #available(macOS 13.0, *) else {
    fputs("macOS 13.0 or later is required\n", stderr)
    exit(1)
}
if CommandLine.arguments.dropFirst().first == "--doctor" {
    let screenAllowed = CGPreflightScreenCaptureAccess()
    let microphoneStatus = AVCaptureDevice.authorizationStatus(for: .audio)
    let microphoneAllowed = microphoneStatus == .authorized
    print(
        "screen_audio=\(screenAllowed ? "authorized" : "missing") "
            + "microphone=\(microphoneAllowed ? "authorized" : String(describing: microphoneStatus))"
    )
    exit(screenAllowed && microphoneAllowed ? 0 : 1)
}
guard CommandLine.arguments.count >= 2 else {
    fputs("Usage: recorder <session-directory>\n", stderr)
    exit(1)
}

let sessionURL = URL(
    fileURLWithPath: CommandLine.arguments[1],
    isDirectory: true
)
let recorder = Recorder(sessionURL: sessionURL)

func stopAndSave() {
    Task { @MainActor in
        do {
            try await recorder.stop()
            exit(0)
        } catch {
            recorder.markFailed(error)
            fputs("Failed to stop recording: \(error.localizedDescription)\n", stderr)
            exit(2)
        }
    }
}

let stdinSource = DispatchSource.makeReadSource(
    fileDescriptor: STDIN_FILENO,
    queue: .main
)
stdinSource.setEventHandler {
    stdinSource.cancel()
    stopAndSave()
}
stdinSource.resume()

var signalSources: [DispatchSourceSignal] = []
for signalNumber in [SIGINT, SIGTERM] {
    signal(signalNumber, SIG_IGN)
    let source = DispatchSource.makeSignalSource(
        signal: signalNumber,
        queue: .main
    )
    source.setEventHandler {
        source.cancel()
        stopAndSave()
    }
    source.resume()
    signalSources.append(source)
}

Task { @MainActor in
    do {
        try await recorder.start()
        fflush(stdout)
    } catch {
        recorder.markFailed(error)
        fputs("Failed to start: \(error.localizedDescription)\n", stderr)
        let message = error.localizedDescription.lowercased()
        if message.contains("not authorized")
            || message.contains("permission")
            || message.contains("access")
        {
            fputs(
                """

                Grant Screen Recording permission to Terminal in:
                System Settings → Privacy & Security → Screen & System Audio Recording

                """,
                stderr
            )
        }
        exit(1)
    }
}

RunLoop.main.run()
