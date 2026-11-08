"""Bounded continuous preprocessing and once-per-window aligned Qwen work.

Three-second slabs preserve AEC/VAD state. Twelve-second cores see three seconds
on each side. A neighbour finalizes the previous word ownership; source closure
flushes the last core. Model and alignment callbacks are deliberately separate so
successful decoding survives an alignment failure. No device or network ownership.
"""
import hashlib

from .consolidation import CORE, CONTEXT, RATE, encode


class ContextualProcessor:
    def __init__(self, reader, journal, decode, align, prepare, observe=None):
        self.reader,self.journal,self.decode,self.align=reader,journal,decode,align
        self.prepare,self.observe=prepare,observe
        self.prepared_position,self.position,self.verified_context_frame=0,0,0
        self.buffer_start,self.buffers,self.presence=0,{},{}
        self.chain,self.failed,self.finished='',False,False

    def _window(self, progress, total):
        import numpy as np
        closed=progress['state']=='closed'
        end=min(self.position+CORE,total)
        if end<=self.position:return None
        right=min(end+CONTEXT,total) if closed else end+CONTEXT
        if self.prepared_position<right:return None
        left=max(0,self.position-CONTEXT)
        reused=decoded=aligned=0
        for source in sorted(self.buffers):
            clip=self.buffers[source][(left-self.buffer_start)//3:(right-self.buffer_start+2)//3]
            digest=hashlib.sha256(self.chain.encode()+clip.tobytes()+encode([source,self.position,end,left,right]).encode()).hexdigest()
            cached=self.journal.begin(source,self.position,end,left,right,digest,closed=closed)
            if cached['committed']:
                reused+=1
                continue
            raw=cached['decoded']
            metrics=[m for m in self.presence[source] if m['end_frame']>self.position and m['start_frame']<end]
            has_voice=True if any(m['has_speech'] is True for m in metrics) else None if any(m['has_speech'] is None for m in metrics) else False
            if raw is None:
                if has_voice is False:
                    raw={'text':'','model_called':False}
                else:
                    raw=self.decode(source,clip,left/RATE,right/RATE)
                    decoded+=1
                raw={**raw,'speech_presence':{'has_speech':has_voice,'blocks':metrics}}
                self.journal.decoded(source,self.position,raw)
            if cached['words'] is None:
                words=self.align(source,clip,raw['text'],left/RATE) if raw['text'].strip() else []
                self.journal.aligned(source,self.position,words)
                aligned+=bool(raw['text'].strip())
            self.journal.commit(source,self.position)
        self.position=end
        # No future cached revision is visible until every source's context hash
        # was checked. A failure in the second channel cannot expose its future.
        self.verified_context_frame=right
        keep=max(0,self.position-CONTEXT)
        for source,signal in self.buffers.items():
            self.buffers[source]=signal[(keep-self.buffer_start)//3:].copy()
            self.presence[source]=[m for m in self.presence[source] if m['end_frame']>keep]
        self.buffer_start=keep
        return {'reused_sources':reused,'decoded_sources':decoded,'aligned_sources':aligned,'end_frame':end}

    def poll(self):
        if self.failed:raise ValueError('consolidation_restart_required')
        progress=self.reader.progress()
        closed=progress['state']=='closed'
        total=(max if closed else min)(progress['sources'].values())
        if self.finished:return {'state':'drained'}
        try:
            outcome=self._window(progress,total)
            if outcome:return {'state':'processed',**outcome}
            end=min(self.prepared_position+CONTEXT,total)
            if end>self.prepared_position and (closed or end-self.prepared_position==CONTEXT and end+63<=total):
                import numpy as np
                pairs={source:self.reader.samples(source,self.prepared_position,end,progress) for source in sorted(progress['sources'])}
                chain=hashlib.sha256(encode([self.chain,{s:p[1] for s,p in pairs.items()}]).encode()).hexdigest()
                signals,metrics=self.prepare({s:p[0] for s,p in pairs.items()})
                if set(signals)!=set(pairs) or set(metrics)!=set(pairs):raise ValueError('consolidation_sources_changed')
                for source,signal in signals.items():
                    if (not isinstance(signal,np.ndarray) or signal.ndim!=1 or len(signal)!=(end-self.prepared_position+2)//3
                            or not np.isfinite(signal).all() or metrics[source].get('has_speech') is not None
                            and type(metrics[source].get('has_speech')) is not bool):
                        raise ValueError('consolidation_preprocessing_invalid')
                if self.observe:self.observe(signals,self.prepared_position,end)
                for source,signal in signals.items():
                    self.buffers[source]=np.concatenate([self.buffers.get(source,np.empty(0,dtype='float32')),signal])
                    self.presence.setdefault(source,[]).append({**metrics[source],'start_frame':self.prepared_position,'end_frame':end})
                self.prepared_position,self.chain=end,chain
                outcome=self._window(progress,total)
                return {'state':'processed',**(outcome or {})}
            if closed and self.prepared_position==total and self.position==total:
                if total:
                    for source in sorted(progress['sources']):self.journal.finish(source,total)
                self.verified_context_frame=total;self.finished=True
                return {'state':'drained'}
            return {'state':'waiting'}
        except BaseException:
            self.failed=True
            raise
