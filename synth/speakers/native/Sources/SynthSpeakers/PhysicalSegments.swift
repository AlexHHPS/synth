import Foundation

/// Community-1's 10 s windows start every 2 s, with a zero-padded final tail.
/// Validate model-window bounds first; never clamp an arbitrary corrupt time.
func physicalSegments(_ raw: [[String: Any]], samples: Int) throws -> [[String: Any]] {
    guard samples > 0, samples <= 86400 * 16000 else { throw SpeakerError.invalidAudio }
    let duration = samples / 16
    let modelEnd = ((samples - 1) / 32000 * 32000 + 160000) / 16
    var result: [[String: Any]] = []
    for segment in raw {
        guard let start = segment["start_ms"] as? Int, let end = segment["end_ms"] as? Int,
              let speaker = segment["speaker_id"] as? String, !speaker.isEmpty,
              start >= 0, start < end, end <= modelEnd + 100 else { throw SpeakerError.invalidAudio }
        if start < duration {
            result.append(["speaker_id": speaker, "start_ms": start, "end_ms": min(end, duration)])
        }
    }
    return result
}
