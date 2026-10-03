from pydantic import BaseModel, ConfigDict, Field, model_validator


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=20_000)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker_id: str | None = None
    employee_id: str | None = None
    source_id: str | None = None


class Transcript(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = Field(default="es", pattern="^es$")
    model_fingerprint: str = Field(min_length=1, max_length=300)
    duration_ms: int = Field(ge=1)
    segments: list[Segment] = Field(min_length=1, max_length=100_000)

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
        return self
