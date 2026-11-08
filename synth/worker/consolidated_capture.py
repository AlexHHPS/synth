"""Private prepared audio and a hash-bound receipt for completed live ASR.

The host can reuse the same audio/words for diarization and publication without
running ASR again. Raw hypotheses remain in the receipt and in the journal.
"""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
import wave

from .consolidation import RATE, encode
from .qwen_runner import group_words

RECEIPT = 'consolidated-capture.json'


def file_hash(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def atomic_json(path, value):
    temporary=path.with_name(path.name+'.'+str(uuid4())+'.part')
    try:
        with os.fdopen(os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w') as stream:
            json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        temporary.replace(path)
        descriptor=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:temporary.unlink(missing_ok=True)


def inference_policy(model_directory, aligner_directory):
    root=Path(__file__).parent
    names=('consolidated_capture.py','qwen_live_runner.py','qwen_runner.py','qwen-model-lock.json','contextual_audio.py',
           'consolidation.py','word_seams.py','incremental_audio.py','echo.py','voice_activity.py','capture_vocabulary.py')
    return {'id':'qwen-core12-context3-shared-ownership-v1','model':str(model_directory),
            'aligner':str(aligner_directory),'source_sha256':{name:file_hash(root/name) for name in names},
            'language':'Spanish','temperature':0.,'tokens_per_second':16,'context_revision':'raw-v1',
            'echo_cancellation':'aec3','speech_detection':'webrtc_rnn','preprocessing_slab_seconds':3}


def fingerprint(policy):
    return 'qwen3-asr-1.7b-context3-aligned:'+hashlib.sha256(encode(policy).encode()).hexdigest()


class PreparedTracks:
    def __init__(self, folder, sources):
        self.folder=Path(folder).resolve(strict=True)
        parent=self.folder/'prepared-context3'
        parent.mkdir(exist_ok=True,mode=0o700)
        if parent.is_symlink():raise ValueError('consolidation_prepared_path_invalid')
        self.directory=parent/str(uuid4());self.directory.mkdir(mode=0o700)
        self.tracks={}
        for source in sources:
            if source not in ('microphone','system'):raise ValueError('consolidation_source_invalid')
            path=self.directory/(source+'.wav')
            stream=os.fdopen(os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600),'w+b')
            audio=wave.open(stream,'wb');audio.setparams((1,2,16000,0,'NONE','not compressed'))
            audio.writeframes(b'');stream.flush();os.fsync(stream.fileno())
            self.tracks[source]={'file':stream,'wave':audio,'path':path,'frames':0}
        self.closed=False

    def append(self, signals, start, end):
        import numpy as np
        if self.closed or set(signals)!=set(self.tracks):raise ValueError('consolidation_prepared_sources_invalid')
        for source,signal in signals.items():
            track=self.tracks[source]
            if track['frames']!=start//3 or len(signal)!=(end-start+2)//3:
                raise ValueError('consolidation_prepared_clock_invalid')
            pcm=np.clip(np.rint(signal*32768),-32768,32767).astype('<i2')
            track['wave'].writeframes(pcm.tobytes())
            track['file'].flush();os.fsync(track['file'].fileno())
            track['frames']+=len(signal)

    def close(self):
        if self.closed:return
        for track in self.tracks.values():
            track['wave'].close();track['file'].flush();os.fsync(track['file'].fileno());track['file'].close()
        self.closed=True

    def descriptors(self):
        self.close()
        return {source:{'path':str(t['path'].relative_to(self.folder)),'sha256':file_hash(t['path']),
                        'frames':t['frames'],'sample_rate':16000} for source,t in self.tracks.items()}


def raw_receipt(folder, progress):
    path=folder/'sources/capture.json'
    if path.is_symlink() or path.stat().st_size>65536:raise ValueError('consolidation_source_receipt_invalid')
    value=json.loads(path.read_text())
    if (value.get('state')!='closed' or value.get('capture_id')!=progress['capture_id']
            or set(value['sources'])!=set(progress['sources'])):
        raise ValueError('consolidation_source_receipt_invalid')
    for source,descriptor in value['sources'].items():
        expected=folder/'sources'/(source+'.wav')
        if (expected.is_symlink() or Path(descriptor['path']).resolve()!=expected.resolve()
                or descriptor['frames']!=progress['sources'][source] or file_hash(expected)!=descriptor['sha256']):
            raise ValueError('consolidation_source_changed')
    return value


def publish(folder, progress, journal, prepared, policy):
    folder=Path(folder).resolve(strict=True)
    if progress['state']!='closed':raise ValueError('consolidation_capture_not_closed')
    raw=raw_receipt(folder,progress)  # may still be being published by the native closer
    total=max(progress['sources'].values())
    sources=prepared.descriptors();transcripts={};evidence=[]
    model=fingerprint(policy)
    for source in progress['sources']:
        cores=journal.cores(source)
        if (not cores or cores[0]['start_frame']!=0 or cores[-1]['end_frame']!=total
                or any(c['revision']!=2 for c in cores)
                or any(a['end_frame']!=b['start_frame'] for a,b in zip(cores,cores[1:]))):
            raise ValueError('consolidation_core_coverage_invalid')
        segments=[];words=[]
        for core in cores:
            result=core['result'];block_id=f"{progress['capture_id']}:{result['block_id']}"
            selected=result['words']
            if words and selected and selected[0]['start_ms']<words[-1]['start_ms']:
                raise ValueError('consolidation_timeline_requires_review')
            grouped=group_words(selected);cursor=0
            for i,segment in enumerate(grouped):
                index=len(segments)
                segments.append({**segment,'id':f"s{index:06d}",'source_id':source,
                                 'asr_block_id':block_id,'evidence_state':result['state']})
                remaining=len(segment['text'].split())
                while remaining:
                    word=selected[cursor];words.append({k:word[k] for k in ('text','start_ms','end_ms')}|{'segment_index':index,'source_id':source})
                    remaining-=len(word['text'].split());cursor+=1
            window=journal.db.execute('SELECT descriptor,decoded FROM windows WHERE source=? AND start_frame=?',
                                      (source,core['start_frame'])).fetchone()
            descriptor=json.loads(window['descriptor']);decoded=json.loads(window['decoded'])
            reviews=[]
            for review in result['reviews']:
                reviews.append({'reason':review.get('reason','seam_disagreement'),'text':review.get('text',''),
                    'start_ms':review.get('start_ms',round(core['start_frame']/48)),
                    'end_ms':review.get('end_ms',round(core['end_frame']/48))})
            evidence.append({'block_id':block_id,'source_id':source,'start_ms':round(descriptor['left']/48),
                'end_ms':round(descriptor['right']/48),'raw_text':decoded['text'],
                'input_sha256':descriptor['sha256'],'model_fingerprint':model,'reviews':reviews})
        transcripts[source]={'language':'es','duration_ms':round(total/48),'model_fingerprint':model,
                             'segments':segments,'words':words}
    receipt={'version':1,'state':'closed','capture_id':progress['capture_id'],'policy':policy,
             'raw_sources':{s:{'sha256':d['sha256'],'frames':d['frames']} for s,d in raw['sources'].items()},
             'prepared_sources':sources,'transcripts':transcripts,'asr_evidence':evidence}
    atomic_json(folder/RECEIPT,receipt)
    return receipt


def load(folder, expected_capture_id, expected_sources):
    """Validate both raw input ownership hashes and the prepared source used by ASR."""
    folder=Path(folder).resolve(strict=True);path=folder/RECEIPT
    if not path.exists():return None
    if path.is_symlink() or path.stat().st_size>32*1024*1024:raise ValueError('consolidation_receipt_invalid')
    value=json.loads(path.read_text())
    if (value.get('version')!=1 or value.get('state')!='closed' or value.get('capture_id')!=expected_capture_id
            or set(value['transcripts'])!=set(expected_sources) or set(value['prepared_sources'])!=set(expected_sources)
            or set(value['raw_sources'])!=set(expected_sources)):
        raise ValueError('consolidation_receipt_invalid')
    for source,descriptor in expected_sources.items():
        path=Path(descriptor['path']).resolve(strict=True)
        if source not in ('microphone','system') or path!=folder/'sources'/(source+'.wav') or file_hash(path)!=descriptor['sha256']:
            raise ValueError('consolidation_source_changed')
        if value['raw_sources'][source]['sha256']!=descriptor['sha256']:
            raise ValueError('consolidation_source_changed')
        with wave.open(str(path)) as audio:
            if (audio.getframerate(),audio.getnchannels(),audio.getsampwidth(),audio.getnframes())!=(48000,1,2,value['raw_sources'][source]['frames']):
                raise ValueError('consolidation_source_changed')
        prepared=folder/value['prepared_sources'][source]['path']
        if (prepared!=prepared.resolve(strict=True) or not prepared.is_relative_to(folder)
                or file_hash(prepared)!=value['prepared_sources'][source]['sha256']):
            raise ValueError('consolidation_prepared_changed')
        with wave.open(str(prepared)) as audio:
            if (audio.getframerate(),audio.getnchannels(),audio.getsampwidth(),audio.getnframes())!=(16000,1,2,value['prepared_sources'][source]['frames']):
                raise ValueError('consolidation_prepared_invalid')
        total=max(s['frames'] for s in value['raw_sources'].values())
        if (value['prepared_sources'][source]['frames']!=(total+2)//3
                or value['transcripts'][source]['duration_ms']!=round(total/48)
                or value['transcripts'][source]['model_fingerprint']!=fingerprint(value['policy'])):
            raise ValueError('consolidation_receipt_invalid')
    return value
