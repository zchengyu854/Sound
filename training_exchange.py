"""File format shared by the Mac workbench and the Ubuntu trainer."""
import hashlib
import json
from typing import Literal
from zipfile import ZIP_STORED

from pydantic import BaseModel, ConfigDict, Field

PACKAGE_VERSION = 1
RECIPE_VERSION = "breeze-lora-recipe-2"
MAX_MANIFEST = 8 * 1024**2
MAX_ADAPTER = 32 * 1024**2
MAX_CLIP = 4 * 1024**2


class TrainingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank: Literal[1, 2, 4, 8] = 4
    steps: int = Field(default=20, ge=1, le=1000)
    learning_rate: float = Field(default=0.0001, ge=0.000001, le=0.001, allow_inf_nan=False)
    accumulation_steps: int = Field(default=1, ge=1, le=8)
    checkpoint_every: int = Field(default=5, ge=1, le=100)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)

    def checked(self):
        return self.model_dump()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def deterministic_order(size, seed, epoch):
    """Reproducible shuffle of the training subset for one epoch."""
    import numpy as np
    generator = np.random.default_rng([int(seed), int(epoch), 0x5EED])
    return [int(value) for value in generator.permutation(size)]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_member(archive, name, limit):
    info = archive.getinfo(name)
    if info.compress_type != ZIP_STORED or not 0 < info.file_size <= limit:
        raise ValueError(f"Invalid package member: {name}")
    with archive.open(info) as member:
        data = member.read(limit + 1)
    if len(data) != info.file_size:
        raise ValueError(f"Invalid package size: {name}")
    return data
