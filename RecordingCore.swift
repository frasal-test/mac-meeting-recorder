import AVFoundation
import CoreGraphics
import CoreMedia
import Foundation
import ScreenCaptureKit

struct RecordingPermissionStatus {
    let screenAudioAllowed: Bool
    let microphoneStatus: AVAuthorizationStatus

    var microphoneAllowed: Bool {
        microphoneStatus == .authorized
    }

    private var microphoneDescription: String {
        switch microphoneStatus {
        case .authorized:
            return "authorized"
        case .denied:
            return "denied"
        case .notDetermined:
            return "not_determined"
        case .restricted:
            return "restricted"
        @unknown default:
            return "unknown"
        }
    }

    var summary: String {
        "screen_audio=\(screenAudioAllowed ? "authorized" : "missing") "
            + "microphone=\(microphoneDescription) "
            + "identity=\(Bundle.main.bundleIdentifier ?? "none")"
    }
}

func recordingPermissionStatus() -> RecordingPermissionStatus {
    RecordingPermissionStatus(
        screenAudioAllowed: CGPreflightScreenCaptureAccess(),
        microphoneStatus: AVCaptureDevice.authorizationStatus(for: .audio)
    )
}

/// macOS attributes TCC grants to the *responsible* process, so this reports
/// the permissions of whoever launched the binary. Run from a shell it
/// describes that shell, not the menu-bar app.
func runPermissionDoctor() -> Never {
    let status = recordingPermissionStatus()
    print(status.summary)
    exit(status.screenAudioAllowed && status.microphoneAllowed ? 0 : 1)
}

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

@available(macOS 13.0, *)
final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private let sessionURL: URL
    private let audioURL: URL
    private let micURL: URL
    private let systemURL: URL
    private let metaURL: URL
    private let showVUMeter: Bool
    private let logger: (String) -> Void

    private var stream: SCStream?
    private var systemFile: AVAudioFile?
    private var audioEngine: AVAudioEngine?
    private var micFile: AVAudioFile?
    private var micFileFormat: AVAudioFormat?
    private var micConverter: AVAudioConverter?
    private var configurationObserver: NSObjectProtocol?

    private var micLevel: Float = 0
    private var systemLevel: Float = 0
    private var vuTimer: DispatchSourceTimer?

    private let stateLock = NSLock()
    private var stopping = false
    private var startedUptime = ProcessInfo.processInfo.systemUptime
    private var metadata: SessionMeta

    init(
        sessionURL: URL,
        showVUMeter: Bool = false,
        logger: @escaping (String) -> Void = { _ in }
    ) {
        self.sessionURL = sessionURL
        self.audioURL = sessionURL.appendingPathComponent(
            "audio",
            isDirectory: true
        )
        self.micURL = audioURL.appendingPathComponent("mic.caf")
        self.systemURL = audioURL.appendingPathComponent("system.caf")
        self.metaURL = sessionURL.appendingPathComponent("meta.json")
        self.showVUMeter = showVUMeter
        self.logger = logger
        self.metadata = SessionMeta(
            state: "starting",
            startedAt: iso8601(),
            trackStartOffsets: TrackOffsets(),
            warnings: []
        )
        super.init()
    }

    private func writeMetadata() throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [
            .prettyPrinted,
            .sortedKeys,
            .withoutEscapingSlashes,
        ]
        try encoder.encode(metadata).write(to: metaURL, options: .atomic)
    }

    private func setTrackOffset(_ track: String) {
        stateLock.lock()
        defer { stateLock.unlock() }
        let offset = max(
            0,
            ProcessInfo.processInfo.systemUptime - startedUptime
        )
        if track == "mic", metadata.trackStartOffsets.mic == nil {
            metadata.trackStartOffsets.mic = offset
            try? writeMetadata()
        } else if track == "system",
                  metadata.trackStartOffsets.system == nil
        {
            metadata.trackStartOffsets.system = offset
            try? writeMetadata()
        }
    }

    func start() async throws {
        try FileManager.default.createDirectory(
            at: audioURL,
            withIntermediateDirectories: true
        )
        startedUptime = ProcessInfo.processInfo.systemUptime
        metadata.state = "recording"
        try writeMetadata()

        await requestMicrophoneAccessIfNeeded()
        try requestScreenCaptureAccessIfNeeded()
        try await startSystemCapture()
        do {
            try startMicCapture()
        } catch {
            let warning =
                "Microphone unavailable; only system audio was recorded: "
                + error.localizedDescription
            metadata.warnings.append(warning)
            try? writeMetadata()
            logger("Warning: \(warning)")
        }
        startVUMeter()
        logger("Recording started")
    }

    private func requestMicrophoneAccessIfNeeded() async {
        guard AVCaptureDevice.authorizationStatus(for: .audio)
            == .notDetermined
        else {
            return
        }
        _ = await AVCaptureDevice.requestAccess(for: .audio)
    }

    private func requestScreenCaptureAccessIfNeeded() throws {
        guard !CGPreflightScreenCaptureAccess() else { return }
        guard CGRequestScreenCaptureAccess() else {
            throw RecorderError.screenPermissionRequired
        }
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
        configuration.minimumFrameInterval = CMTime(
            seconds: 1,
            preferredTimescale: 1
        )

        let filter = SCContentFilter(
            display: display,
            excludingApplications: [],
            exceptingWindows: []
        )
        let captureStream = SCStream(
            filter: filter,
            configuration: configuration,
            delegate: self
        )
        stream = captureStream
        try captureStream.addStreamOutput(
            self,
            type: .audio,
            sampleHandlerQueue: DispatchQueue(
                label: "taprecord.system-audio"
            )
        )
        try await captureStream.startCapture()
    }

    /// The tap must always use the input node's own output format. Forcing a
    /// channel count or reusing a sample rate read before the engine starts
    /// breaks on real hardware: the Razer Seiren X reports two channels, and a
    /// Bluetooth headset renegotiates down to 16 kHz the moment the microphone
    /// opens. Anything the tap delivers is converted into a fixed mono file
    /// format, so a mid-session device change never corrupts the track.
    private func startMicCapture() throws {
        let engine = AVAudioEngine()
        audioEngine = engine

        let tapFormat = engine.inputNode.outputFormat(forBus: 0)
        guard tapFormat.sampleRate > 0, tapFormat.channelCount > 0 else {
            throw RecorderError.micFormatUnavailable
        }
        guard
            let fileFormat = AVAudioFormat(
                standardFormatWithSampleRate: tapFormat.sampleRate,
                channels: 1
            )
        else {
            throw RecorderError.micFormatUnavailable
        }

        micFileFormat = fileFormat
        micFile = try AVAudioFile(
            forWriting: micURL,
            settings: fileFormat.settings
        )
        try installMicTap(on: engine)
        observeConfigurationChanges(for: engine)
        try engine.start()
    }

    private func installMicTap(on engine: AVAudioEngine) throws {
        guard let fileFormat = micFileFormat else { return }
        let inputNode = engine.inputNode
        let tapFormat = inputNode.outputFormat(forBus: 0)
        guard tapFormat.sampleRate > 0, tapFormat.channelCount > 0 else {
            throw RecorderError.micFormatUnavailable
        }

        if tapFormat == fileFormat {
            micConverter = nil
        } else {
            guard
                let converter = AVAudioConverter(
                    from: tapFormat,
                    to: fileFormat
                )
            else {
                throw RecorderError.micFormatUnavailable
            }
            micConverter = converter
        }

        inputNode.installTap(
            onBus: 0,
            bufferSize: 4096,
            format: tapFormat
        ) { [weak self] buffer, _ in
            self?.appendMicAudio(buffer)
        }
        logger(
            "Microphone tap: \(describe(tapFormat)) → \(describe(fileFormat))"
        )
    }

    private func appendMicAudio(_ buffer: AVAudioPCMBuffer) {
        guard !stopping, let file = micFile, let fileFormat = micFileFormat
        else {
            return
        }
        if metadata.trackStartOffsets.mic == nil {
            setTrackOffset("mic")
        }

        let output: AVAudioPCMBuffer
        if let converter = micConverter {
            let ratio = fileFormat.sampleRate / buffer.format.sampleRate
            let capacity =
                AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
            guard
                let converted = AVAudioPCMBuffer(
                    pcmFormat: fileFormat,
                    frameCapacity: capacity
                )
            else {
                return
            }
            var supplied = false
            var conversionError: NSError?
            let status = converter.convert(
                to: converted,
                error: &conversionError
            ) { _, outStatus in
                if supplied {
                    outStatus.pointee = .noDataNow
                    return nil
                }
                supplied = true
                outStatus.pointee = .haveData
                return buffer
            }
            if status == .error {
                logger(
                    "Microphone conversion error: "
                        + (conversionError?.localizedDescription ?? "unknown")
                )
                return
            }
            guard converted.frameLength > 0 else { return }
            output = converted
        } else {
            output = buffer
        }

        do {
            try file.write(from: output)
        } catch {
            logger("Microphone write error: \(error.localizedDescription)")
        }
        if showVUMeter {
            micLevel = rms(buffer: output)
        }
    }

    /// macOS posts this when the default input changes or a device is
    /// added/removed. Without reinstalling the tap the microphone goes silent
    /// for the rest of the session.
    private func observeConfigurationChanges(for engine: AVAudioEngine) {
        configurationObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: .main
        ) { [weak self, weak engine] _ in
            guard let self, let engine, !self.stopping else { return }
            self.logger("Audio configuration changed; reattaching microphone")
            engine.inputNode.removeTap(onBus: 0)
            do {
                try self.installMicTap(on: engine)
                if !engine.isRunning {
                    try engine.start()
                }
            } catch {
                self.logger(
                    "Could not reattach the microphone: "
                        + error.localizedDescription
                )
            }
        }
    }

    private func appendSystemAudio(_ sampleBuffer: CMSampleBuffer) {
        let frameCount = CMSampleBufferGetNumSamples(sampleBuffer)
        guard frameCount > 0 else { return }
        guard
            let description = CMSampleBufferGetFormatDescription(sampleBuffer),
            let streamDescription =
                CMAudioFormatDescriptionGetStreamBasicDescription(description),
            let format = AVAudioFormat(
                streamDescription: streamDescription
            ),
            let pcmBuffer = AVAudioPCMBuffer(
                pcmFormat: format,
                frameCapacity: AVAudioFrameCount(frameCount)
            )
        else {
            logger("Could not decode the system audio format")
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
            logger("System audio conversion error: \(status)")
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
            if showVUMeter {
                systemLevel = rms(buffer: pcmBuffer)
            }
        } catch {
            logger("System audio write error: \(error.localizedDescription)")
        }
    }

    private func startVUMeter() {
        guard showVUMeter else { return }
        print("")
        let timer = DispatchSource.makeTimerSource(queue: .main)
        vuTimer = timer
        timer.schedule(deadline: .now(), repeating: .milliseconds(150))
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            print(
                "\r  🎤 \(vuBar(self.micLevel))  "
                    + "🔊 \(vuBar(self.systemLevel))  ",
                terminator: ""
            )
            fflush(stdout)
        }
        timer.resume()
    }

    private func stopVUMeter() {
        vuTimer?.cancel()
        vuTimer = nil
        if showVUMeter {
            print("")
        }
    }

    func stop() async throws {
        guard !stopping else { return }
        stopping = true
        stopVUMeter()

        if let observer = configurationObserver {
            NotificationCenter.default.removeObserver(observer)
            configurationObserver = nil
        }
        audioEngine?.inputNode.removeTap(onBus: 0)
        audioEngine?.stop()
        micFile = nil
        micConverter = nil
        micFileFormat = nil

        try? await stream?.stopCapture()
        systemFile = nil

        metadata.state = "recorded"
        metadata.endedAt = iso8601()
        metadata.durationSeconds = max(
            0,
            ProcessInfo.processInfo.systemUptime - startedUptime
        )
        try writeMetadata()
        logger("Recording stopped")
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
        logger("Recording failed: \(error.localizedDescription)")
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio, !stopping else { return }
        appendSystemAudio(sampleBuffer)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        logger("Stream error: \(error.localizedDescription)")
    }

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

private func describe(_ format: AVAudioFormat) -> String {
    "\(Int(format.sampleRate))Hz/\(format.channelCount)ch"
}

private func vuBar(_ rms: Float, width: Int = 12) -> String {
    let db = 20 * log10(max(rms, 1e-9))
    let level = min(max((db + 60) / 60, 0), 1)
    let filled = Int(level * Float(width))
    return String(repeating: "█", count: filled)
        + String(repeating: "░", count: width - filled)
}

enum RecorderError: Error, LocalizedError {
    case noDisplay
    case micFormatUnavailable
    case screenPermissionRequired

    var errorDescription: String? {
        switch self {
        case .noDisplay:
            return "No display found"
        case .micFormatUnavailable:
            return "Could not create the microphone audio format"
        case .screenPermissionRequired:
            return "Screen & System Audio permission is required. Enable "
                + "MeetRec in System Settings → Privacy & Security → "
                + "Screen & System Audio Recording, then restart MeetRec."
        }
    }
}
