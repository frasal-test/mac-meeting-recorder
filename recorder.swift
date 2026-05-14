import AVFoundation
import Foundation
import ScreenCaptureKit

// MARK: - Recorder

@available(macOS 13.0, *)
final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private let finalURL: URL
    private let systemTmpURL: URL
    private let micTmpURL: URL

    // System audio (ScreenCaptureKit)
    private var stream: SCStream?
    private var systemWriter: AVAssetWriter?
    private var systemInput: AVAssetWriterInput?
    private var systemSessionStarted = false

    // Microphone (AVAudioEngine)
    private var audioEngine: AVAudioEngine?
    private var micFile: AVAudioFile?

    // VU meter (microphone only)
    private var micLevel: Float = 0
    private var vuTimer: DispatchSourceTimer?

    private var stopping = false

    init(url: URL) {
        self.finalURL = url
        let base = url.deletingLastPathComponent()
            .appendingPathComponent(url.deletingPathExtension().lastPathComponent)
        self.systemTmpURL = URL(fileURLWithPath: base.path + "._sys.m4a")
        self.micTmpURL    = URL(fileURLWithPath: base.path + "._mic.caf")
        super.init()
    }

    // MARK: - Start

    func start() async throws {
        try await startSystemCapture()
        do {
            try startMicCapture()
        } catch {
            fputs("Warning: microphone unavailable — recording system audio only. (\(error.localizedDescription))\n", stderr)
        }
        startVUMeter()
    }

    private func startSystemCapture() async throws {
        let content = try await SCShareableContent.current
        guard let display = content.displays.first else {
            throw RecorderError.noDisplay
        }

        systemWriter = try AVAssetWriter(outputURL: systemTmpURL, fileType: .m4a)
        systemInput = AVAssetWriterInput(mediaType: .audio, outputSettings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 48_000.0,
            AVNumberOfChannelsKey: 2,
            AVEncoderBitRateKey: 192_000,
        ])
        systemInput!.expectsMediaDataInRealTime = true
        systemWriter!.add(systemInput!)
        systemWriter!.startWriting()

        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(seconds: 1, preferredTimescale: 1)

        let filter = SCContentFilter(
            display: display, excludingApplications: [], exceptingWindows: [])
        stream = SCStream(filter: filter, configuration: cfg, delegate: self)
        try stream!.addStreamOutput(self, type: .audio, sampleHandlerQueue: .global())
        try await stream!.startCapture()
    }

    private func startMicCapture() throws {
        audioEngine = AVAudioEngine()
        let inputNode = audioEngine!.inputNode

        // Request standard Float32 mono regardless of the hardware format.
        // AVAudioEngine converts automatically, and floatChannelData is always
        // non-nil for standard-format buffers so the VU meter always works.
        let hwRate = inputNode.inputFormat(forBus: 0).sampleRate
        guard let format = AVAudioFormat(standardFormatWithSampleRate: hwRate, channels: 1) else {
            throw RecorderError.micFormatUnavailable
        }

        micFile = try AVAudioFile(forWriting: micTmpURL, settings: format.settings)
        inputNode.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            guard let self, !self.stopping else { return }
            try? self.micFile?.write(from: buffer)
            self.micLevel = self.rms(buffer: buffer)
        }
        try audioEngine!.start()
    }

    // MARK: - VU meter

    private func startVUMeter() {
        print("")  // blank line that the VU meter will overwrite
        vuTimer = DispatchSource.makeTimerSource(queue: .main)
        vuTimer!.schedule(deadline: .now(), repeating: .milliseconds(150))
        vuTimer!.setEventHandler { [weak self] in
            guard let self else { return }
            let mic = vuBar(self.micLevel)
            print("\r  🎤 \(mic)  ", terminator: "")
            fflush(stdout)
        }
        vuTimer!.resume()
    }

    private func stopVUMeter() {
        vuTimer?.cancel()
        vuTimer = nil
        print("")  // newline after the VU meter line
    }

    // MARK: - Stop

    func stop() async {
        guard !stopping else { return }
        stopping = true

        stopVUMeter()

        // Stop microphone
        audioEngine?.inputNode.removeTap(onBus: 0)
        audioEngine?.stop()
        micFile = nil

        // Stop system audio
        try? await stream?.stopCapture()
        systemInput?.markAsFinished()
        await withCheckedContinuation { cont in
            systemWriter?.finishWriting { cont.resume() }
        }

        do {
            try await mergeAudio()
        } catch {
            fputs("Merge error: \(error.localizedDescription)\n", stderr)
        }

        try? FileManager.default.removeItem(at: systemTmpURL)
        try? FileManager.default.removeItem(at: micTmpURL)
    }

    // MARK: - Merge

    private func mergeAudio() async throws {
        let composition = AVMutableComposition()

        func addTrack(from url: URL) async throws {
            let asset = AVURLAsset(url: url)
            let tracks = try await asset.loadTracks(withMediaType: .audio)
            guard let track = tracks.first else { return }
            let duration = try await asset.load(.duration)
            let compTrack = composition.addMutableTrack(
                withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)
            try compTrack?.insertTimeRange(
                CMTimeRange(start: .zero, duration: duration), of: track, at: .zero)
        }

        if FileManager.default.fileExists(atPath: systemTmpURL.path) { try await addTrack(from: systemTmpURL) }
        if FileManager.default.fileExists(atPath: micTmpURL.path)    { try await addTrack(from: micTmpURL) }

        guard !composition.tracks.isEmpty else { throw RecorderError.noAudioCaptured }

        guard let export = AVAssetExportSession(
            asset: composition, presetName: AVAssetExportPresetAppleM4A)
        else { throw RecorderError.exportFailed }

        try await export.export(to: finalURL, as: .m4a)
    }

    // MARK: - SCStreamOutput

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer buf: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio, !stopping else { return }

        guard let input = systemInput, input.isReadyForMoreMediaData else { return }
        if !systemSessionStarted {
            systemWriter?.startSession(atSourceTime: buf.presentationTimeStamp)
            systemSessionStarted = true
        }
        input.append(buf)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("Stream error: \(error.localizedDescription)\n", stderr)
    }

    // MARK: - Level helpers

    private func rms(buffer: AVAudioPCMBuffer) -> Float {
        guard let data = buffer.floatChannelData?[0] else { return 0 }
        let n = Int(buffer.frameLength)
        guard n > 0 else { return 0 }
        var sum: Float = 0
        for i in 0..<n { sum += data[i] * data[i] }
        return sqrt(sum / Float(n))
    }

}

// MARK: - VU bar

private func vuBar(_ rms: Float, width: Int = 12) -> String {
    let db = 20 * log10(max(rms, 1e-9))
    let level = min(max((db + 60) / 60, 0), 1)
    let filled = Int(level * Float(width))
    return String(repeating: "█", count: filled) + String(repeating: "░", count: width - filled)
}

// MARK: - Errors

enum RecorderError: Error, LocalizedError {
    case noDisplay
    case noAudioCaptured
    case exportFailed
    case micFormatUnavailable

    var errorDescription: String? {
        switch self {
        case .noDisplay:            return "No display found"
        case .noAudioCaptured:      return "No audio was captured"
        case .exportFailed:         return "Could not create export session"
        case .micFormatUnavailable: return "Could not create Float32 mic format"
        }
    }
}

// MARK: - Entry point

guard #available(macOS 13.0, *) else {
    fputs("macOS 13.0 or later is required\n", stderr)
    exit(1)
}

guard CommandLine.arguments.count >= 2 else {
    fputs("Usage: recorder <output.m4a>\n", stderr)
    exit(1)
}

let outputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let recorder  = Recorder(url: outputURL)

func stopAndSave() {
    Task { @MainActor in
        await recorder.stop()
        exit(0)
    }
}

// Stop on Enter (used by meet.sh)
let stdinSource = DispatchSource.makeReadSource(fileDescriptor: STDIN_FILENO, queue: .main)
stdinSource.setEventHandler {
    stdinSource.cancel()
    stopAndSave()
}
stdinSource.resume()

// Stop on Ctrl+C or SIGTERM (used standalone)
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler {
        src.cancel()
        stopAndSave()
    }
    src.resume()
}

Task { @MainActor in
    do {
        try await recorder.start()
        fflush(stdout)
    } catch {
        fputs("Failed to start: \(error.localizedDescription)\n", stderr)
        let msg = error.localizedDescription.lowercased()
        if msg.contains("not authorized") || msg.contains("permission") || msg.contains("access") {
            fputs("\nGrant Screen Recording permission to Terminal in:\n", stderr)
            fputs("System Settings → Privacy & Security → Screen & System Audio Recording\n", stderr)
        }
        exit(1)
    }
}

dispatchMain()
