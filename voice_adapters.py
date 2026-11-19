"""Validation for standalone Breeze LoRA result packages."""
from collections import Counter
import io
import json
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from training_exchange import (
    MAX_ADAPTER,
    MAX_MANIFEST,
    PACKAGE_VERSION,
    RECIPE_VERSION,
    TrainingConfig,
    read_member,
    sha256,
)

MAX_ARTIFACT = MAX_MANIFEST + MAX_ADAPTER + 1024**2


class ImportedAdapter(BaseModel):
    model_config = ConfigDict(extra="allow")

    format: int
    recipe_version: str
    job_id: UUID
    attempt: int = Field(ge=1)
    base_model: str = Field(min_length=1)
    config: TrainingConfig
    base_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial_adapter_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    adapter_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    steps: int = Field(ge=1, le=1000)


def read_adapter_package(data, expected_base_model):
    """Return the validated manifest and adapter bytes from an Ubuntu result ZIP."""
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            if Counter(archive.namelist()) != {
                "result.json": 1,
                "adapter.safetensors": 1,
            }:
                raise ValueError
            raw_manifest = read_member(archive, "result.json", MAX_MANIFEST)
            adapter = read_member(archive, "adapter.safetensors", MAX_ADAPTER)
        result = ImportedAdapter.model_validate(json.loads(raw_manifest))
        if (
            result.format != PACKAGE_VERSION
            or result.recipe_version != RECIPE_VERSION
            or result.base_model != expected_base_model
            or result.steps != result.config.steps
            or result.adapter_sha256 != sha256(adapter)
        ):
            raise ValueError
    except (
        BadZipFile,
        EOFError,
        KeyError,
        OSError,
        RecursionError,
        RuntimeError,
        TypeError,
        ValueError,
        ValidationError,
    ) as exc:
        raise ValueError("训练产物无效、已损坏，或与当前基座不兼容。") from exc

    manifest = {
        "base_model": result.base_model,
        "rank": result.config.rank,
        "base_digest": result.base_digest,
        "sha256": result.adapter_sha256,
    }
    return {
        "manifest": manifest,
        "config": result.config.checked(),
        "job_id": str(result.job_id),
        "attempt": result.attempt,
        "adapter_sha256": result.adapter_sha256,
    }, adapter
