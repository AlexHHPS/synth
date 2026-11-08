"""Select auditable, disjoint voice evidence without retaining extra recordings."""
from synth.worker.voice_windows import RATE, WINDOW, GUARD, merge, clean_intervals, spaced_offsets, voice_windows

IDENTITY_POLICY = 'guarded_disjoint_employee_votes_090_v1'


def configured_space():
    import json
    from synth.worker.qwen_asr import CONFIG
    configuration = json.loads(CONFIG.read_text()) if CONFIG.is_file() else {}
    if not isinstance(configuration,dict):
        raise ValueError('speaker_embedding_space_invalid')
    space = configuration.get('voice_embedding_space','cosine256')
    if space not in ('cosine256','plda_rho128_v1'):
        raise ValueError('speaker_embedding_space_invalid')
    return space


def analysis_policy(space=None):
    return IDENTITY_POLICY + ':' + (space or configured_space())
