from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=20_000)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker_id: str | None = None
    employee_id: str | None = None
    source_id: str | None = None
    asr_block_id: str | None = Field(default=None, max_length=150)
    evidence_state: Literal['aligned', 'needs_review'] | None = None

    @model_serializer(mode="wrap")
    def omit_absent_asr_evidence(self, handler):
        data = handler(self)
        for optional in ('asr_block_id', 'evidence_state'):
            if data.get(optional) is None:
                data.pop(optional, None)
        return data


class Turn(BaseModel):
    """A readable speaker turn built from word timings; `segment_ids` keep old citations resolvable."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^t\d{6}$")
    text: str = Field(min_length=1, max_length=60_000)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker_id: str | None = None
    employee_id: str | None = None
    source_id: str | None = None
    segment_ids: list[str] = Field(default_factory=list, max_length=5_000)
    interjection: bool = False
    word_count: int = Field(ge=0)


class Speaker(BaseModel):
    """Owner-resolved speaker. Unconfirmed suggestions never enter a transcript."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["confirmed", "renamed", "rejected"]
    name: str | None = Field(default=None, min_length=1, max_length=200)
    employee_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def named_only_when_resolved(self):  # slop-ok: pydantic validator, called by the model
        if self.status == "rejected" and (self.name or self.employee_id):
            raise ValueError("rejected_speaker_has_no_name")
        if self.status != "rejected" and not self.name:
            raise ValueError("resolved_speaker_needs_name")
        return self


class Correction(BaseModel):
    """A glossary correction on top of the raw ASR: logged, reversible, never on the raw version."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^c\d{4}$")
    segment_id: str = Field(min_length=1, max_length=100)
    original: str = Field(min_length=1, max_length=200)
    replacement: str = Field(min_length=1, max_length=200)
    term: str = Field(min_length=1, max_length=120)
    # A suggestion leaves the text as heard until a person accepts it; a rejection is kept per meeting
    # (correction_reviews), not in the immutable version.
    status: Literal["applied", "undone", "suggested"] = "applied"
    # Who decided it: the text model (opt-in), a registered «sounds like» alias, or an ambiguous alias the meeting's
    # own mention of the term confirmed.
    origin: Literal["modelo", "alias", "contexto"] | None = None
    # Where the replacement sits now, so undo and the UI act on that occurrence and not on another spelling of it.
    start: int | None = Field(default=None, ge=0)
    turn_id: str | None = Field(default=None, min_length=1, max_length=100)
    turn_start: int | None = Field(default=None, ge=0)


class ASRReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reason: str = Field(min_length=1, max_length=100)
    text: str = Field(default='', max_length=20_000)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class ASREvidence(BaseModel):
    """Original overlapping hypotheses, stored for review, not added to spoken text."""
    model_config = ConfigDict(extra='forbid')
    block_id: str = Field(pattern=r'^[0-9a-f-]{36}:(microphone|system):\d+$', max_length=150)
    source_id: Literal['microphone', 'system']
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    raw_text: str = Field(max_length=20_000)
    input_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    model_fingerprint: str = Field(min_length=1, max_length=300)
    reviews: list[ASRReview] = Field(default_factory=list, max_length=1024)


class Transcript(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = Field(default="es", pattern="^es$")
    model_fingerprint: str = Field(min_length=1, max_length=300)
    duration_ms: int = Field(ge=1)
    segments: list[Segment] = Field(min_length=1, max_length=100_000)
    turns: list[Turn] | None = Field(default=None, max_length=100_000)
    speakers: dict[str, Speaker] | None = None
    corrections: list[Correction] | None = Field(default=None, max_length=5_000)
    raw_version: int | None = Field(default=None, ge=1)
    asr_evidence: list[ASREvidence] | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def ordered_unique_segments(self):
        ids, previous = set(), -1
        for segment in self.segments:
            if (segment.id in ids or segment.start_ms < previous or
                    segment.end_ms < segment.start_ms or segment.end_ms > self.duration_ms or
                    not segment.text.strip()):
                raise ValueError("invalid_transcript_timeline")
            ids.add(segment.id)
            previous = segment.start_ms
        if self.turns is not None:
            turn_ids, previous = set(), -1
            for turn in self.turns:
                if (turn.id in turn_ids or turn.start_ms < previous or turn.end_ms < turn.start_ms or
                        turn.end_ms > self.duration_ms or not turn.text.strip() or
                        any(i not in ids for i in turn.segment_ids)):
                    raise ValueError("invalid_transcript_turns")
                turn_ids.add(turn.id)
                previous = turn.start_ms
        if self.asr_evidence is not None:
            blocks = {}
            for row in self.asr_evidence:
                if (row.block_id in blocks or row.block_id.split(':')[1] != row.source_id
                        or not row.start_ms <= row.end_ms <= self.duration_ms
                        or row.model_fingerprint != self.model_fingerprint
                        or any(not 0 <= r.start_ms <= r.end_ms <= self.duration_ms for r in row.reviews)):
                    raise ValueError('invalid_asr_evidence')
                blocks[row.block_id] = row
            for segment in self.segments:
                if segment.asr_block_id is not None and (
                        segment.asr_block_id not in blocks or segment.evidence_state is None
                        or blocks[segment.asr_block_id].source_id != segment.source_id):
                    raise ValueError('invalid_asr_segment_evidence')
        if any((s.asr_block_id is None) != (s.evidence_state is None) for s in self.segments):
            raise ValueError('invalid_asr_segment_evidence')
        if self.asr_evidence is None and any(s.asr_block_id is not None for s in self.segments):
            raise ValueError('asr_evidence_missing')
        return self

    @model_serializer(mode="wrap")
    def omit_absent_turns(self, handler):  # slop-ok: pydantic validator, called by the model
        # Transcripts without turns serialize exactly as before (same content hash).
        data = handler(self)
        for optional in ("turns", "speakers", "corrections", "raw_version", "asr_evidence"):
            if data.get(optional) is None:
                data.pop(optional, None)
        return data
