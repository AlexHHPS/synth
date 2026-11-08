import CoreML
import CryptoKit
import Darwin
import Foundation
import FluidAudio

enum ResidentError: Error { case request, path, key }

/// One private pipe, one model set, ordered calls. Never a network listener.
final class ResidentSpeakers {
    private let manager: OfflineDiarizerManager
    private let projection: PLDATransform
    private let directory: URL

    init(models root: URL, directory: URL) throws {
        self.directory = URL(fileURLWithPath: try residentCanonicalPath(directory.path))
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: self.directory.path, isDirectory: &isDirectory),
              isDirectory.boolValue else { throw ResidentError.path }
        let models = try OfflineDiarizerModels(
            segmentationModel: SynthSpeakers.model(root, "Segmentation.mlmodelc"),
            fbankModel: SynthSpeakers.model(root, "FBank.mlmodelc", cpu: true),
            embeddingModel: SynthSpeakers.model(root, "Embedding.mlmodelc"),
            pldaRhoModel: SynthSpeakers.model(root, "PldaRho.mlmodelc"),
            pldaPsi: SynthSpeakers.psi(root), compilationDuration: 0)
        manager = OfflineDiarizerManager(config: OfflineDiarizerConfig(exposeChunkEmbeddings: true))
        manager.initialize(models: models)
        projection = PLDATransform(pldaRhoModel: try SynthSpeakers.model(root, "PldaRho.mlmodelc", cpu: true), psi: [])
    }

    private func location(_ value: Any?) throws -> URL {
        try residentLocation(value, directory: directory)
    }

    private func normalized(_ vector: [Double], count: Int) throws -> [Double] {
        let norm = sqrt(vector.reduce(0) { $0 + $1 * $1 })
        guard vector.count == count, vector.allSatisfy(\.isFinite), norm.isFinite, norm > 1e-8
        else { throw SpeakerError.invalidEmbedding }
        return vector.map { $0 / norm }
    }

    private func embed(_ path: URL) async throws -> (vector: [Float], duration: Int) {
        let samples = try AudioConverter().resampleAudioFile(path: path.path)
        guard samples.count >= 48_000, samples.count <= 160_000 else { throw SpeakerError.invalidAudio }
        let result = try await manager.process(audio: samples)
        guard Set(result.segments.map { $0.speakerId }).count == 1,
              let vector = result.chunkEmbeddings?.first?.embedding256,
              vector.count == 256, vector.allSatisfy(\.isFinite), vector.contains(where: { $0 != 0 })
        else { throw SpeakerError.invalidEmbedding }
        return (vector, samples.count / 16)
    }

    private func key(_ text: Any?) throws -> SymmetricKey {
        guard let text = text as? String, text.count == 64,
              text.allSatisfy({ "0123456789abcdef".contains($0) }) else { throw ResidentError.key }
        let chars = Array(text)
        let bytes = stride(from: 0, to: 64, by: 2).compactMap { UInt8(String(chars[$0...$0 + 1]), radix: 16) }
        guard bytes.count == 32 else { throw ResidentError.key }
        return SymmetricKey(data: Data(bytes))
    }

    private func sealed(_ value: [String: Any], key: SymmetricKey, binding: String, output: URL) throws -> [String: Any] {
        let plaintext = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        let encrypted = try AES.GCM.seal(plaintext, using: key, authenticating: Data(binding.utf8))
        guard let combined = encrypted.combined else { throw ResidentError.key }
        let record: [String: Any] = ["version": 1, "binding": binding, "combined": combined.base64EncodedString()]
        let bytes = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
        try bytes.write(to: output, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: output.path)
        let file = try FileHandle(forWritingTo: output)
        try file.synchronize(); try file.close()
        let fd = open(output.deletingLastPathComponent().path, O_RDONLY)
        guard fd >= 0 else { throw ResidentError.path }
        defer { close(fd) }
        guard fsync(fd) == 0 else { throw ResidentError.path }
        return ["path": output.path, "sha256": SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined(),
                "sealed": true]
    }

    private func execute(_ request: [String: Any]) async throws -> [String: Any] {
        guard let operation = request["operation"] as? String else { throw ResidentError.request }
        let allowed: Set<String>
        switch operation {
        case "diarize": allowed = ["id", "operation", "input"]
        case "embed": allowed = ["id", "operation", "input", "seal"]
        case "project": allowed = ["id", "operation", "vectors"]
        default: throw ResidentError.request
        }
        guard Set(request.keys).isSubset(of: allowed) else { throw ResidentError.request }
        if operation == "project" {
            guard let vectors = request["vectors"] else { throw ResidentError.request }
            let data = try JSONSerialization.data(withJSONObject: vectors)
            let projected = try await projection.transform(projectionInputs(data))
            guard projected.allSatisfy({ $0.count == 128 && $0.allSatisfy(\.isFinite) })
            else { throw SpeakerError.invalidEmbedding }
            return ["vectors": projected, "embedding_space": "plda_rho128_v1"]
        }
        let path = try location(request["input"])
        if operation == "diarize" {
            let samples = try AudioConverter().resampleAudioFile(path: path.path)
            guard !samples.isEmpty, samples.count <= 45 * 16000 else { throw SpeakerError.invalidAudio }
            do {
                let result = try await manager.process(audio: samples)
                let raw = result.segments.map {
                    ["speaker_id": $0.speakerId, "start_ms": Int($0.startTimeSeconds * 1000),
                     "end_ms": Int($0.endTimeSeconds * 1000)] as [String: Any]
                }
                // FluidAudio reconstructs every 10 s segmentation window,
                // including its explicitly zero-padded tail. Publish only the
                // intersection with physical samples and retain the raw spans.
                let bounded = try physicalSegments(raw, samples: samples.count)
                return ["speech_state": bounded.isEmpty ? "no_usable_speech" : "detected", "segments": bounded,
                        "raw_segments": raw, "time_policy": "intersection_with_physical_audio_v1"]
            } catch OfflineDiarizationError.noSpeechDetected {
                return ["speech_state": "no_usable_speech", "segments": []]
            }
        }
        var sealKey: SymmetricKey?
        var binding: String?
        var output: URL?
        if let seal = request["seal"] as? [String: Any] {
            guard Set(seal.keys) == ["key_hex", "binding", "output"],
                  let text = seal["binding"] as? String, !text.isEmpty, text.utf8.count <= 32768
            else { throw ResidentError.request }
            sealKey = try key(seal["key_hex"]); binding = text; output = try location(seal["output"])
            guard !FileManager.default.fileExists(atPath: output!.path) else { throw ResidentError.path }
        } else if request["seal"] != nil { throw ResidentError.request }
        let embedded = try await embed(path)
        if let sealKey, let binding, let output {
            // Existing profiles/project_embeddings use unit raw vectors before
            // PLDA. Projecting unnormalised model output changes that space.
            let unit = try normalized(embedded.vector.map(Double.init), count: 256)
            let projected = try await projection.transform([unit.map(Float.init)])
            guard let rho = projected.first, projected.count == 1 else { throw SpeakerError.invalidEmbedding }
            let value: [String: Any] = ["embedding": unit,
                "rho128": try normalized(rho, count: 128), "duration_ms": embedded.duration,
                "revision": SynthSpeakers.libraryRevision]
            return try sealed(value, key: sealKey, binding: binding, output: output)
        }
        return ["embedding": embedded.vector, "duration_ms": embedded.duration]
    }

    private func send(_ value: [String: Any]) {
        guard var bytes = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return }
        bytes.append(10)
        FileHandle.standardOutput.write(bytes)
    }

    func serve() async {
        send(["ready": true, "protocol_version": 1, "revision": SynthSpeakers.libraryRevision, "model_loads": 1])
        while let line = readLine() {
            var identifier = ""
            do {
                guard line.utf8.count <= 1_048_576,
                      let request = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                      let id = request["id"] as? String, UUID(uuidString: id) != nil
                else { throw ResidentError.request }
                identifier = id
                if request["operation"] as? String == "stop" && Set(request.keys) == ["id", "operation"] {
                    send(["id": id, "stopped": true]); return
                }
                let began = Date()
                let result = try await execute(request)
                send(result.merging(["id": id, "elapsed_seconds": Date().timeIntervalSince(began)]) { _, new in new })
            } catch {
                // Request/path/key failures and model failures have stable codes.
                // No exception payload, key, recording or vector is logged.
                let code = error is ResidentError ? "speaker_session_request_invalid" : "speaker_inference_failed"
                send(["id": identifier, "error_code": code])
            }
        }
    }
}
