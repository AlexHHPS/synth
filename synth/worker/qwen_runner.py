"""Isolated, offline MLX runner. One JSON request on stdin, one result on stdout.

Only this process loads MLX. The packaged host retains its existing Python stack.
References, speaker identities and domain corrections are never accepted here.
"""
import contextlib
import gc
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import wave


ALIGNMENT_FAULTS = {'qwen_alignment_invalid_time', 'qwen_alignment_nonmonotonic',
                    'qwen_alignment_incomplete', 'qwen_alignment_text_mismatch'}
APPROXIMATE = 3


def estimated_words(text, offset, duration):
    """Spread the recognised words over their clip in proportion to their length.

    Used only when the aligner cannot place them. The text is untouched and
    every time stays inside the clip, so the words still land on the right
    speaker turn to within a few seconds.
    """
    pieces = text.split()
    total = sum(len(piece) for piece in pieces) or 1
    words, position = [], 0
    for piece in pieces:
        start = offset + duration * position / total
        position += len(piece)
        end = offset + duration * position / total
        words.append({'text': piece, 'start_ms': round(start * 1000), 'end_ms': round(end * 1000)})
    return words


def bounded_words(text, items, processor, offset, duration):
    """Keep the aligner's order and clamp its times into the clip."""
    last, bounded = 0.0, []
    for item in items:
        start = min(max(float(item.start_time), last), duration)
        end = min(max(float(item.end_time), start), duration)
        bounded.append(SimpleNamespace(text=item.text, start_time=start, end_time=end))
        last = start
    return restore_punctuation(text, bounded, processor, offset, duration)


def align_chunk(aligner, clip, text, offset):
    """Place words in time. A clip the aligner cannot place never fails the meeting.

    Returns (words, effort): 0 direct, 1-2 after giving the model silence
    around a clipped boundary, 3 when the times had to be bounded or estimated.
    """
    import numpy as np
    duration = len(clip) / 16000
    attempts = (
        lambda: aligner.generate(clip, text=text, language='Spanish').items,
        # Forced alignment occasionally places the last word past a clipped
        # boundary; trailing silence gives it context without changing the source.
        lambda: aligner.generate(np.pad(clip, (0, 16000)), text=text, language='Spanish').items,
        # Clipped leading phonemes need context on both edges; the exact
        # leading offset is removed before validation.
        lambda: [SimpleNamespace(text=i.text, start_time=i.start_time-1, end_time=i.end_time-1)
                 for i in aligner.generate(np.pad(clip, (16000, 16000)), text=text, language='Spanish').items],
    )
    first = None
    for effort, attempt in enumerate(attempts):
        items = attempt()
        if first is None:
            first = items
        try:
            return restore_punctuation(text, items, aligner.aligner_processor, offset, duration), effort
        except ValueError as error:
            if str(error) not in ALIGNMENT_FAULTS:
                raise
            if str(error) in ('qwen_alignment_text_mismatch', 'qwen_alignment_incomplete'):
                # The aligner split the words differently; more audio context cannot change that.
                break
    try:
        return bounded_words(text, first, aligner.aligner_processor, offset, duration), APPROXIMATE
    except (ValueError, TypeError, IndexError):
        return estimated_words(text, offset, duration), APPROXIMATE


def save_decoded(path, content):
    path = Path(path)
    temporary = path.with_suffix('.part')
    with os.fdopen(os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), 'w') as stream:
        json.dump(content, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def audio_ranges(samples):
    """Cover every input frame once, cutting near quiet 100 ms boundaries."""
    import numpy as np
    start, total = 0, len(samples)
    while start < total:
        end = min(start + 30 * 16000, total)
        if end < total:
            candidates = range(end - 2 * 16000, end, 1600)
            end = min(candidates, key=lambda p: float(np.mean(samples[p:p+1600] ** 2))) + 800
        yield start, end
        start = end


def decode_chunks(samples, generate, *, cache=None, cache_key=None, presence=None):
    """Bound each generation and durably resume successful source intervals.

    Token exhaustion is never accepted as a transcript. Only that interval is
    subdivided, with a finite retry tree; no speech is silently discarded.
    """
    import numpy as np
    saved = {'version': 2, 'cache_key': cache_key, 'frames': len(samples), 'chunks': {}, 'splits': {}}
    if cache and cache_key:
        try:
            candidate = json.loads(Path(cache).read_text())
            if (candidate.get('version') == 2 and candidate.get('cache_key') == cache_key
                    and candidate.get('frames') == len(samples)
                    and isinstance(candidate.get('chunks'), dict) and isinstance(candidate.get('splits'), dict)):
                saved = candidate
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def persist():
        if cache and cache_key:
            save_decoded(cache, saved)

    def decode(start, end, has_speech):
        name = f'{start}:{end}'
        limit = max(128, math.ceil((end-start)/16000*16))
        item = saved['chunks'].get(name)
        if (isinstance(item, dict) and isinstance(item.get('text'), str)
                and type(item.get('generation_tokens')) is int
                and 0 <= item['generation_tokens'] < limit
                and item.get('speech_presence') is has_speech):
            return [{'start': start/16000, 'end': end/16000, 'text': item['text'],
                     'generation_tokens': item['generation_tokens'], 'speech_presence': has_speech}]
        middle = saved['splits'].get(name)
        if type(middle) is int and start + 16000 <= middle <= end - 16000:
            return decode(start, middle, has_speech) + decode(middle, end, has_speech)
        clip = samples[start:end]
        if not np.any(clip) or has_speech is False:
            item = {'text': '', 'generation_tokens': 0, 'speech_presence': has_speech}
        else:
            result = generate(clip, language='Spanish', chunk_duration=30.0,
                              max_tokens=limit, temperature=0.0, prefill_step_size=512, verbose=False)
            if result.generation_tokens >= limit and end-start > 5*16000:
                middle = (start+end)//2
                saved['splits'][name] = middle
                persist()
                return decode(start, middle, has_speech) + decode(middle, end, has_speech)
            if result.generation_tokens >= limit:
                # A few seconds the model cannot finish (it loops on them) must
                # not cost the whole meeting. Their text is withheld, never
                # published half-decoded, and the span is marked for review.
                item = {'text': '', 'generation_tokens': 0, 'speech_presence': has_speech,
                        'decode_incomplete': True}
                saved['chunks'][name] = item
                persist()
                return [{'start': start/16000, 'end': end/16000, **item}]
            if (not isinstance(result.text, str) or type(result.generation_tokens) is not int
                    or result.generation_tokens < 0
                    or ' '.join(s['text'] for s in (result.segments or [])).split() != result.text.split()):
                raise ValueError('qwen_asr_incomplete')
            item = {'text': result.text.strip(), 'generation_tokens': result.generation_tokens,
                    'speech_presence': has_speech}
        saved['chunks'][name] = item
        persist()
        return [{'start': start/16000, 'end': end/16000, **item}]

    chunks = []
    for start, end in audio_ranges(samples):
        # Advance the detector over original audio even when decoding is cached;
        # retries reuse the parent's evidence, including cold-start uncertainty.
        evidence = presence(samples[start:end]) if presence else None
        chunks.extend(decode(start, end, evidence))
    return {'text': ' '.join(s['text'] for s in chunks if s['text']), 'chunks': chunks,
            'generation_tokens': sum(s['generation_tokens'] for s in chunks),
            'incomplete_seconds': sum(s['end']-s['start'] for s in chunks if s.get('decode_incomplete'))}


def restore_punctuation(text, items, processor, offset, duration):
    """Apply alignment times to the original ASR tokens without rewriting them."""
    expected = processor.tokenize_space_lang(text)
    if expected != [item.text for item in items]:
        raise ValueError('qwen_alignment_text_mismatch')
    words, cursor, prefix = [], 0, []
    for piece in text.split():
        count = len(processor.tokenize_space_lang(piece))
        if not count:
            if words:
                words[-1]['text'] += ' ' + piece
            else:
                prefix.append(piece)
            continue
        first, last = items[cursor], items[cursor + count - 1]
        start, end = first.start_time, last.end_time
        if not 0 <= start <= end <= duration + .2:
            raise ValueError('qwen_alignment_invalid_time')
        start_ms = round((offset + min(start, duration)) * 1000)
        end_ms = round((offset + min(end, duration)) * 1000)
        if words and start_ms < words[-1]['start_ms']:
            raise ValueError('qwen_alignment_nonmonotonic')
        words.append({'text': ' '.join(prefix + [piece]), 'start_ms': start_ms, 'end_ms': end_ms})
        prefix = []
        cursor += count
    if cursor != len(items) or prefix:
        raise ValueError('qwen_alignment_incomplete')
    if ' '.join(w['text'] for w in words) != ' '.join(text.split()):
        raise ValueError('qwen_alignment_text_mismatch')
    return words


def group_words(words):
    """Short phrase spans for the existing speaker-overlap adapter."""
    groups, current = [], []
    for word in words:
        if current and word['start_ms'] - current[-1]['end_ms'] > 600:
            groups.append(current)
            current = []
        current.append(word)
        if word['text'].endswith(('.', '?', '!', ';', ':')) or word['end_ms'] - current[0]['start_ms'] >= 4000:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return [{'text': ' '.join(w['text'] for w in group), 'start_ms': group[0]['start_ms'],
             'end_ms': max(w['end_ms'] for w in group)} for group in groups]


def run(request):
    import mlx.core as mx
    import numpy as np
    from mlx_audio.stt.utils import load_model

    lock = json.loads(Path(__file__).with_name('qwen-model-lock.json').read_text())
    for package, version in lock['runtime'].items():
        if importlib.metadata.version(package) != version:
            raise ValueError('qwen_runtime_version_mismatch')
    mx.set_memory_limit(12 * 1024**3)
    mx.set_cache_limit(1024**3)
    started = time.monotonic()
    with wave.open(request['audio']) as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
            raise ValueError('asr_input_must_be_mono_16khz_pcm16')
        samples = np.frombuffer(audio.readframes(audio.getnframes()), '<i2').astype('float32') / 32768
    cache = request.get('decoded_cache')
    # Import only this bundled, trusted package root in the isolated interpreter.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from synth.worker.voice_activity import SpeechPresence
    detector = SpeechPresence()
    model = None
    def generate(*args, **kwargs):
        nonlocal model
        if model is None:
            model = load_model(request['model'])
        if request.get('system_prompt'):
            kwargs['system_prompt'] = request['system_prompt']
        result = model.generate(*args, **kwargs)
        mx.clear_cache()
        return result
    decoded = decode_chunks(samples, generate, cache=cache, cache_key=request.get('cache_key'),
                            presence=lambda clip: detector.analyze('system', clip)['has_speech'])
    if model is not None:
        del model
        gc.collect()
        mx.clear_cache()
    chunks = decoded['chunks']
    if ' '.join(s['text'] for s in chunks).split() != decoded['text'].split():
        raise ValueError('qwen_asr_incomplete')
    words = []
    alignment_retries = approximate_chunks = 0
    aligner = load_model(request['aligner']) if any(s['text'].strip() for s in chunks) else None
    for chunk in chunks:
        text = chunk['text'].strip()
        if not text:
            continue
        start, end = float(chunk['start']), float(chunk['end'])
        clip = samples[round(start*16000):round(end*16000)]
        if not len(clip) or end-start > 250:
            raise ValueError('qwen_alignment_invalid_chunk')
        aligned_words, retries = align_chunk(aligner, clip, text, start)
        words.extend(aligned_words)
        alignment_retries += retries
        approximate_chunks += retries == APPROXIMATE
    return {'text': decoded['text'], 'segments': group_words(words), 'words': words,
            'elapsed_seconds': time.monotonic()-started, 'peak_mlx_gb': mx.get_peak_memory()/1e9,
            'asr_chunks': len(chunks), 'generation_tokens': decoded['generation_tokens'],
            'alignment_retries': alignment_retries, 'approximate_chunks': approximate_chunks}


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            result = run(request)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        code = str(error)
        if not (code.startswith(('qwen_', 'asr_')) and code.replace('_', '').isalnum()):
            code = 'qwen_inference_failed'
        print(json.dumps({'error_code': code, 'exception': type(error).__name__}))
        sys.exit(1)
