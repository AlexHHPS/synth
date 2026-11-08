import Foundation
import Testing
@testable import SynthSpeakers

@Test func normalizedProjectionInputKeepsItsDimensions() throws {
    let vector = [1.0] + [Double](repeating: 0, count: 255)
    let data = try JSONEncoder().encode([vector, vector])
    #expect(try projectionInputs(data).map(\.count) == [256, 256])
}

@Test func malformedEmptyOversizedOrZeroProjectionInputsAreRejected() throws {
    for vectors in [[], [[Double](repeating: 0, count: 256)], [[1.0, 2.0]],
                    [[Double](repeating: 2, count: 256)],
                    [[Double]](repeating: [1]+[Double](repeating: 0, count: 255), count: 129)] {
        let data = try JSONEncoder().encode(vectors)
        #expect(throws: (any Error).self) { try projectionInputs(data) }
    }
}
