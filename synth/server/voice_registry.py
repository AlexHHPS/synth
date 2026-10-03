"""Corporate speaker centroids only. Audio is never accepted by this contract."""
import base64
import json
import os
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from synth.speakers.profiles import normalized


class Enrollment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    model: str = Field(min_length=1, max_length=100)
    embedding: list[float] = Field(min_length=256, max_length=256)
    consent_confirmed: bool

    @field_validator("embedding")
    @classmethod
    def validate_vector(cls, value):
        return normalized(value)


def cipher():
    try:
        source = os.environ.get('VOICE_PROFILE_ENCRYPTION_KEY_FILE')
        value = Path(source).read_text().strip() if source else os.environ['VOICE_PROFILE_ENCRYPTION_KEY']
        key = base64.b64decode(value, validate=True)
        if len(key) != 32:
            raise ValueError()
    except (KeyError, ValueError, OSError):
        raise HTTPException(503, "El registro de voz no está configurado.") from None
    return AESGCM(key)


def aad(profile, owner, model):
    return (str(profile) + ":" + str(owner) + ":" + model).encode()


def seal(body, owner):
    nonce = os.urandom(12)
    payload = json.dumps(normalized(body.embedding), separators=(",", ":")).encode()
    return nonce, cipher().encrypt(nonce, payload, aad(body.id, owner, body.model))


def unseal(row):
    payload = cipher().decrypt(bytes(row["nonce"]), bytes(row["ciphertext"]),
                               aad(row["id"], row["owner_id"], row["model"]))
    return normalized(json.loads(payload))
