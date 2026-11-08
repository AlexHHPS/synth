"""Local and server contract for pseudonymous, rooted voice samples. No audio."""
import math
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from synth.speakers.profiles import normalized


class Sample(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: UUID
    model: str = Field(min_length=1, max_length=100)
    embedding: list[float] = Field(min_length=256, max_length=256)
    consent_confirmed: bool

    @field_validator('embedding')
    @classmethod
    def validate_vector(cls, value):
        return normalized(value)

    root_profile_id: UUID
    capture_id: UUID
    group_id: str = Field(min_length=1, max_length=200)
    source: str = Field(pattern='^(microphone|system|import)$')
    source_ranges: list[tuple[int, int]] = Field(min_length=1, max_length=64)
    evidence_ids: list[str] = Field(min_length=3, max_length=3)
    origin: str = Field(pattern='^(enrolled_high_confidence|human_confirmation)$')
    vote_similarities: list[float] = Field(min_length=0, max_length=2)
    vote_margins: list[float] = Field(min_length=0, max_length=2)
    vote_evidence_ids: list[str] = Field(min_length=0, max_length=2)
    vote_crop_seconds: int = Field(ge=5, le=10)
    confirmation_id: UUID | None = None
    policy: str = Field(pattern='^rooted_independent_crop10_v1$')

    @field_validator('evidence_ids')
    @classmethod
    def validate_ids(cls, value):
        if len(set(value)) != 3 or any(len(x) != 64 or any(c not in '0123456789abcdef' for c in x) for x in value):
            raise ValueError('voice_learning_evidence_invalid')
        return value

    @model_validator(mode='after')
    def validate_lineage(self):
        previous, duration = -1, 0
        for start, end in self.source_ranges:
            if not 0 <= start < end <= 86400*16000 or start < previous:
                raise ValueError('voice_learning_ranges_invalid')
            previous, duration = end, duration+end-start
        if duration != 10*16000 or not self.model.endswith('#crop10s'):
            raise ValueError('voice_learning_crop_invalid')
        values = [*self.vote_similarities, *self.vote_margins]
        if any(not math.isfinite(v) or not -2 <= v <= 2 for v in values):
            raise ValueError('voice_learning_confidence_invalid')
        if self.origin == 'enrolled_high_confidence':
            if (len(self.vote_similarities) != 2 or len(self.vote_margins) != 2
                    or min(self.vote_similarities) < .90 or min(self.vote_margins) < .10
                    or self.confirmation_id is not None or self.vote_crop_seconds not in (5,10)
                    or len(set(self.vote_evidence_ids)) != 2
                    or any(len(x)!=64 or any(c not in '0123456789abcdef' for c in x) for x in self.vote_evidence_ids)
                    or self.vote_crop_seconds == 5 and self.vote_evidence_ids != self.evidence_ids[:2]
                    or self.vote_crop_seconds == 10 and self.evidence_ids[-1] not in self.vote_evidence_ids):
                raise ValueError('voice_learning_anchor_not_strong')
        elif self.confirmation_id is None:
            raise ValueError('voice_learning_confirmation_missing')
        return self

