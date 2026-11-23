"""Versioned official-model observations for locally reproducible data gaps."""

from __future__ import annotations

from functools import cache
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .artifacts import Matrix, Vector


class RelativePose(BaseModel):
    position: Vector  # raw LDraw coordinates in LDU, in the host part frame
    rotation: Matrix


class GapReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: Literal["P-001", "P-007"]
    gap: Literal["missing_endpoint", "endpoint_offset"]
    host: str
    child: str
    catalog_version: str
    catalog_sha256: str
    source_url: str
    source_sha256: str
    reference_sha256: str
    reference_lines: str
    tolerance: float
    candidate_count: int
    poses: tuple[RelativePose, ...]
    witness: str


class GapReferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    references: tuple[GapReference, ...]


@cache
def gap_references() -> tuple[GapReference, ...]:
    data = (files(__package__) / "gap_references.json").read_bytes()
    return GapReferences.model_validate_json(data).references
