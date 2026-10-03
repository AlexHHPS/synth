"""Association proof only: no invented vectors or segments count as acoustic proof."""
from synth.speakers.native import attach_speaker_labels

transcript = {"segments": [
    {"id": "clear", "start_ms": 0, "end_ms": 1000, "text": "Una frase", "speaker_id": "stale", "employee_id": "stale"},
    {"id": "mixed", "start_ms": 1000, "end_ms": 2000, "text": "Otra frase", "speaker_id": "stale"},
    {"id": "silence", "start_ms": 2000, "end_ms": 3000, "text": "Texto", "speaker_id": "stale"},
]}
turns = {"segments": [
    {"start_ms": 0, "end_ms": 1500, "speaker_id": "S1"},
    {"start_ms": 1400, "end_ms": 2000, "speaker_id": "S2"},
]}
result = attach_speaker_labels(transcript, turns)
assert result["segments"][0]["speaker_id"] == "S1"
assert result["segments"][1]["speaker_id"] is None
assert result["segments"][2]["speaker_id"] is None
assert all(s["employee_id"] is None for s in result["segments"])
assert all(a["text"] == b["text"] for a, b in zip(result["segments"], transcript["segments"]))
assert transcript["segments"][0]["speaker_id"] == "stale"
print("SPEAKER_ALIGNMENT_WALK_OK; ambiguous and silent spans abstain; input unchanged")
