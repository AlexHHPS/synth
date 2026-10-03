import Foundation
import CoreML
import FluidAudio

enum SpeakerError: Error { case usage, invalidModel, invalidAudio, invalidEmbedding }

@main
struct SynthSpeakers {
    static let libraryRevision = "c388107348134698135cfd34f3f59dc823b6e7ce"

    static func model(_ root: URL, _ name: String, cpu: Bool = false) throws -> MLModel {
        let url = root.appendingPathComponent(name)
        guard FileManager.default.fileExists(atPath: url.path) else { throw SpeakerError.invalidModel }
        let config = MLModelConfiguration()
        config.computeUnits = cpu ? .cpuOnly : .all
        // Direct local loading: missing files fail, never download during inference.
        return try MLModel(contentsOf: url, configuration: config)
    }

    static func psi(_ root: URL) throws -> [Double] {
        let data = try Data(contentsOf: root.appendingPathComponent("plda-parameters.json"))
        guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let tensors = obj["tensors"] as? [String: Any],
              let psi = tensors["psi"] as? [String: Any],
              let encoded = psi["data_base64"] as? String,
              let raw = Data(base64Encoded: encoded), !raw.isEmpty, raw.count % 4 == 0
        else { throw SpeakerError.invalidModel }
        return stride(from: 0, to: raw.count, by: 4).map { i in
            let bits = UInt32(raw[i]) | UInt32(raw[i+1]) << 8 | UInt32(raw[i+2]) << 16 | UInt32(raw[i+3]) << 24
            return Double(Float(bitPattern: bits))
        }
    }

    static func save(_ object: [String: Any], _ path: String) throws {
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        let url = URL(fileURLWithPath: path)
        try data.write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    static func main() async {
        do {
            let args = CommandLine.arguments
            guard args.count == 5 else { throw SpeakerError.usage }
            let operation = args[1], input = args[2], root = URL(fileURLWithPath: args[3]), output = args[4]
                let models = try OfflineDiarizerModels(
                    segmentationModel: model(root, "Segmentation.mlmodelc"),
                    fbankModel: model(root, "FBank.mlmodelc", cpu: true),
                    embeddingModel: model(root, "Embedding.mlmodelc"),
                    pldaRhoModel: model(root, "PldaRho.mlmodelc"),
                    pldaPsi: psi(root), compilationDuration: 0)
                let manager = OfflineDiarizerManager(config: OfflineDiarizerConfig(exposeChunkEmbeddings: true))
                manager.initialize(models: models)
            if operation == "diarize" {
                do {
                    let result = try await manager.process(URL(fileURLWithPath: input))
                    let segments: [[String: Any]] = result.segments.map {
                        ["speaker_id": $0.speakerId, "start_ms": Int($0.startTimeSeconds * 1000),
                         "end_ms": Int($0.endTimeSeconds * 1000)]
                    }
                    try save(["mode": "real", "engine": "FluidAudio-offline-VBx", "revision": libraryRevision,
                              "speech_state": "detected", "segments": segments], output)
                } catch OfflineDiarizationError.noSpeechDetected {
                    // An empty embedding set is a normal outcome for a silent/noisy
                    // source, not a model or capture failure. Never catch other errors.
                    try save(["mode": "real", "engine": "FluidAudio-offline-VBx", "revision": libraryRevision,
                              "speech_state": "no_usable_speech", "segments": []], output)
                }
            } else if operation == "embed" {
                let samples = try AudioConverter().resampleAudioFile(path: input)
                guard samples.count >= 48_000, samples.count <= 160_000 else { throw SpeakerError.invalidAudio }
                let result = try await manager.process(audio: samples)
                guard Set(result.segments.map { $0.speakerId }).count == 1 else { throw SpeakerError.invalidAudio }
                let embeddings = result.chunkEmbeddings?.map { $0.embedding256 } ?? []
                guard let vector = embeddings.first, vector.count == 256,
                      vector.allSatisfy({ $0.isFinite }), vector.contains(where: { $0 != 0 })
                else { throw SpeakerError.invalidEmbedding }
                try save(["mode": "real", "engine": "Community1-WeSpeaker-CoreML", "revision": libraryRevision,
                          "duration_ms": samples.count / 16, "embedding": vector], output)
            } else { throw SpeakerError.usage }
            print("SYNTH_SPEAKER_OUTPUT_SAVED")
        } catch {
            // No raw audio, embeddings or model exception payload in user logs.
            FileHandle.standardError.write(Data("Speaker processing failed; check local input and model installation.\n".utf8))
            exit(1)
        }
    }
}
