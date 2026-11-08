import Foundation

func projectionInputs(_ data: Data) throws -> [[Float]] {
    let vectors = try JSONDecoder().decode([[Double]].self, from: data)
    guard !vectors.isEmpty, vectors.count <= 128 else { throw SpeakerError.invalidEmbedding }
    return try vectors.map { vector in
        guard vector.count == 256, vector.allSatisfy({ $0.isFinite && abs($0) <= 1.001 }),
              vector.contains(where: { $0 != 0 }) else { throw SpeakerError.invalidEmbedding }
        return vector.map(Float.init)
    }
}
