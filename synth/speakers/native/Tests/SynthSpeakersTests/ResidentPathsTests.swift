import Foundation
import XCTest
@testable import SynthSpeakers

final class ResidentPathsTests: XCTestCase {
    func testExistingInputAndFutureCiphertextUseTheSameCanonicalTemporaryRoot() throws {
        let temporary = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: temporary) }
        let root = URL(fileURLWithPath: try residentCanonicalPath(temporary.path))
        let input = root.path + "/input.wav"
        let output = root.path + "/output.sealed.json"
        try Data([0]).write(to: URL(fileURLWithPath: input))
        XCTAssertEqual(try residentLocation(input, directory: root).path, input)
        XCTAssertEqual(try residentLocation(output, directory: root).path, output)
        try Data([1]).write(to: try residentLocation(output, directory: root))
        XCTAssertEqual(try residentLocation(output, directory: root).path, output)
    }

    func testSymlinksTraversalAndSiblingPrefixesRemainDenied() throws {
        let temporary = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: temporary) }
        let root = URL(fileURLWithPath: try residentCanonicalPath(temporary.path))
        let input = root.path + "/input.wav"
        try Data([0]).write(to: URL(fileURLWithPath: input))
        let alias = root.path + "/alias.wav"
        try FileManager.default.createSymbolicLink(atPath: alias, withDestinationPath: input)
        XCTAssertThrowsError(try residentLocation(alias, directory: root))
        let broken = root.path + "/broken.wav"
        try FileManager.default.createSymbolicLink(atPath: broken, withDestinationPath: root.path + "/missing")
        XCTAssertThrowsError(try residentLocation(broken, directory: root))
        XCTAssertThrowsError(try residentLocation(root.path + "/../outside", directory: root))
        XCTAssertThrowsError(try residentLocation(root.path + "-other/output.json", directory: root))
    }
}
