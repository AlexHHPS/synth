"""Local speech presence before ASR; not speaker recognition or word validation."""
import importlib.metadata

POLICY = "webrtc_rnn_presence_10ms_max_gt_0_5_cold_uncertain_v3"


class SpeechPresence:
    def __init__(self):
        if importlib.metadata.version("pywebrtc-audio") != "0.2.0":
            raise ValueError("echo_runtime_version_mismatch")
        self.detectors = {}
        self.seen = set()

    def analyze(self, source, audio):
        import numpy as np
        from pywebrtc_audio import VoiceDetector
        if (source not in ("microphone", "system") or not isinstance(audio, np.ndarray)
                or audio.ndim != 1 or not 0 < len(audio) <= 30*16000 or not np.isfinite(audio).all()):
            raise ValueError("incremental_voice_activity_input_invalid")
        if source not in self.detectors:
            self.detectors[source] = VoiceDetector()
        detector = self.detectors[source]
        probabilities = [detector.process(np.pad(audio[i:i+160], (0, max(0, 160-len(audio[i:i+160])))))
                         for i in range(0, len(audio), 160)]
        maximum = max(probabilities)
        if not all(np.isfinite(p) and 0 <= p <= 1 for p in probabilities):
            raise ValueError("incremental_voice_activity_output_invalid")
        # The detector missed short speech at capture start in the local probe.
        # A low first-block score is uncertainty, not proof of silence. Preserve
        # that ASR and mark it for later review; never delete a possible negation.
        cold = source not in self.seen
        self.seen.add(source)
        present = True if maximum > .5 else (None if cold else False)
        return {"policy": POLICY, "has_speech": present,
                "state": "uncertain_start" if present is None else ("speech" if present else "no_speech"),
                "max_probability": maximum,
                "positive_frame_ms": sum(p > .5 for p in probabilities)*10}
