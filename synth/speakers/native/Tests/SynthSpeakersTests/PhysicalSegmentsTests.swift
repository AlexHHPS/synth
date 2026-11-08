import XCTest
@testable import SynthSpeakers

final class PhysicalSegmentsTests: XCTestCase {
    func test_known_padded_tail_is_intersected_but_not_invented_as_source_audio() throws {
        let spans: [[String: Any]] = [
            ["speaker_id": "S1", "start_ms": 0, "end_ms": 5110],
            ["speaker_id": "S2", "start_ms": 5200, "end_ms": 5500],
        ]
        let result = try physicalSegments(spans, samples: 5 * 16000)
        XCTAssertEqual(result.count, 1)
        XCTAssertEqual(result[0]["end_ms"] as? Int, 5000)
        XCTAssertEqual(spans[0]["end_ms"] as? Int, 5110)
    }

    func test_corrupt_times_are_rejected_before_physical_intersection() {
        for span in [["speaker_id": "S1", "start_ms": -1, "end_ms": 100],
                     ["speaker_id": "S1", "start_ms": 0, "end_ms": 60000],
                     ["speaker_id": "S1", "start_ms": 100, "end_ms": 100]] as [[String: Any]] {
            XCTAssertThrowsError(try physicalSegments([span], samples: 5 * 16000))
        }
    }

    func test_long_recording_tail_uses_the_same_physical_clock() throws {
        for minutes in [20, 60] {
            let duration = minutes * 60 * 1000
            let span: [[String: Any]] = [["speaker_id": "S1", "start_ms": duration - 500,
                                         "end_ms": duration + 110]]
            let result = try physicalSegments(span, samples: minutes * 60 * 16000)
            XCTAssertEqual(result[0]["start_ms"] as? Int, duration - 500)
            XCTAssertEqual(result[0]["end_ms"] as? Int, duration)
        }
    }
}
