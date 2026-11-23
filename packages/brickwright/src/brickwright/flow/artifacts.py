"""Versioned inputs for the staged build flow."""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from brickwright.tools.diagnostics import Diagnostic

if TYPE_CHECKING:
    from brickwright.record import Record

VERSION = 1
Artifact = Literal["features", "plan", "review"]
NonEmpty = Annotated[str, Field(min_length=1)]
Vector = tuple[float, float, float]
Matrix = tuple[Vector, Vector, Vector]


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1]


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: NonEmpty
    text: NonEmpty


class Conditions(Document):
    task_id: str | None
    task_sha256: str | None
    checks: list[Check]


def original_conditions(task_bytes: bytes | None) -> Conditions:
    if task_bytes is None:
        return Conditions(schema_version=1, task_id=None, task_sha256=None, checks=[])
    task = json.loads(task_bytes)
    checks = task.get("checks") if isinstance(task, dict) else None
    if not isinstance(checks, list) or any(
        not isinstance(text, str) or not text.strip() for text in checks
    ):
        raise ValueError("task.json must contain a checks array of nonempty strings")
    task_id = task.get("id", task.get("task_id"))
    return Conditions(
        schema_version=1,
        task_id=str(task_id) if task_id is not None else None,
        task_sha256=sha256(task_bytes).hexdigest(),
        checks=[
            Check(id=f"C{index:02d}", text=text) for index, text in enumerate(checks, 1)
        ],
    )


class FeatureEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_id: NonEmpty | None = None
    image: NonEmpty | None = None
    view: NonEmpty | None = None
    region: NonEmpty | None = None

    @model_validator(mode="after")
    def check_source(self):
        if self.check_id is None and not (self.image and self.view and self.region):
            raise ValueError("Provide check_id or an image, view and region")
        if self.image is not None and not (self.view and self.region):
            raise ValueError("Image evidence requires a view and region")
        return self


class Feature(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: NonEmpty
    name: NonEmpty
    description: NonEmpty
    category: Literal["retain", "simplify"]
    importance: Literal["required", "preferred"]
    evidence: list[FeatureEvidence] = Field(min_length=1)
    rationale: NonEmpty
    simplification: NonEmpty | None = None

    @model_validator(mode="after")
    def check_simplification(self):
        if self.category == "simplify" and not self.simplification:
            raise ValueError("A planned simplification requires a description")
        return self


class Features(Document):
    features: list[Feature] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        _ensure_unique([feature.id for feature in self.features])
        return self


class Bounds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minimum: Vector
    maximum: Vector

    @model_validator(mode="after")
    def ordered_bounds(self):
        if any(lo >= hi for lo, hi in zip(self.minimum, self.maximum, strict=True)):
            raise ValueError("minimum must be below maximum on each axis")
        return self


class Instance(BaseModel):
    """Identify one flattened placement in BrickLayer world coordinates (studs, studs, bricks)."""

    model_config = ConfigDict(extra="forbid")
    part_id: NonEmpty
    position: Vector
    rotation: Matrix
    path: list[str | int] | None = None


class IndependentObject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: NonEmpty
    purpose: NonEmpty
    members: list[Instance] = Field(min_length=1)


class DecorativeInterface(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: NonEmpty
    host: Instance
    child: Instance
    required: bool = True
    gap: Literal["endpoint_offset", "missing_endpoint", "collision_model"] | None = None
    source: Literal["P-001", "P-007"] | None = None
    reason: NonEmpty
    feature_id: NonEmpty | None = None

    @model_validator(mode="after")
    def evidence_source(self):
        if self.gap and not self.source:
            raise ValueError("A data-gap claim requires a P-001 or P-007 reproduction")
        return self


class Plan(Document):
    bounds: Bounds
    colors: list[NonEmpty] = Field(min_length=1)
    independent_objects: list[IndependentObject] = Field(default_factory=list)
    decorative_interfaces: list[DecorativeInterface] = Field(default_factory=list)
    modules: list[dict[str, Any]] = Field(default_factory=list)
    interfaces: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self):
        _ensure_unique([obj.id for obj in self.independent_objects])
        _ensure_unique([interface.id for interface in self.decorative_interfaces])
        return self


class Review(Document):
    """Envelope used by the independent review driver and documented in status."""

    model_config = ConfigDict(extra="allow")
    round_id: NonEmpty


_MODELS = {"features": Features, "plan": Plan, "review": Review}


def _ensure_unique(ids: list[str]) -> None:
    if len(ids) != len(set(ids)):
        raise ValueError("IDs must be unique")


def file_sha256(path: Path) -> str | None:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def atomic_write(path: Path, data: bytes) -> None:
    """Replace one file without exposing a partial JSON document to readers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temp_path = Path(temporary.name)
        try:
            temporary.write(data)
            temporary.close()
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)


def document_bytes(document: BaseModel) -> bytes:
    return (document.model_dump_json(indent=2) + "\n").encode()


def _command(record: Record, name: str) -> str:
    return f"brickwright external {name} {record.id} --set {name}.json"


def artifact_error(
    record: Record,
    name: Artifact | Literal["conditions"],
    message: str,
    **evidence: Any,
) -> Diagnostic:
    return Diagnostic(
        code=f"flow.{name}",
        stage="flow",
        message=message,
        evidence=evidence,
        next_action=(
            "Initialize a flow record using the original task.json."
            if name == "conditions"
            else _command(record, name)
        ),
    )


def read_conditions(record: Record) -> Conditions:
    path = record.path / "conditions.json"
    data = path.read_bytes()
    if sha256(data).hexdigest() != record.read_meta().conditions_sha256:
        raise ValueError("conditions.json differs from the registered original")
    conditions = Conditions.model_validate_json(data)
    task = record.path / "task.json"
    original = original_conditions(task.read_bytes() if task.exists() else None)
    if conditions != original:
        raise ValueError(
            "task.json differs from the source used to register the conditions"
        )
    return conditions


def _feature_errors(record: Record, document: Features) -> list[Diagnostic]:
    conditions = read_conditions(record)
    expected = {check.id for check in conditions.checks}
    problems = []
    covered = set()
    for index, feature in enumerate(document.features):
        for item, evidence in enumerate(feature.evidence):
            check_id = evidence.check_id
            if check_id is None:
                continue
            path = f"$.features[{index}].evidence[{item}].check_id"
            if check_id not in expected:
                problems.append(
                    artifact_error(
                        record,
                        "features",
                        "Unknown original condition.",
                        path=path,
                        expected=sorted(expected),
                        actual=check_id,
                    )
                )
            elif feature.importance != "required" or feature.category != "retain":
                problems.append(
                    artifact_error(
                        record,
                        "features",
                        "Original conditions must be retained as required features.",
                        path=f"$.features[{index}]",
                        expected=["required", "retain"],
                        actual=[feature.importance, feature.category],
                    )
                )
            else:
                covered.add(check_id)
    if missing := sorted(expected - covered):
        problems.append(
            artifact_error(
                record,
                "features",
                "Every original condition must be represented.",
                path="$.features",
                expected=sorted(expected),
                actual=sorted(covered),
                missing=missing,
            )
        )
    return problems


def _plan_errors(record: Record, document: Plan) -> list[Diagnostic]:
    diagnostics = []
    optional = [
        interface
        for interface in document.decorative_interfaces
        if not interface.required
    ]
    if optional:
        try:
            features = Features.model_validate_json(
                (record.path / "features.json").read_bytes()
            )
            eligible = {
                feature.id
                for feature in features.features
                if feature.category == "simplify" and feature.importance == "preferred"
            }
        except (OSError, ValidationError):
            eligible = set()
        for interface in optional:
            if interface.feature_id not in eligible:
                diagnostics.append(
                    artifact_error(
                        record,
                        "plan",
                        "An optional decorative interface must refer to a preferred feature with a declared simplification.",
                        path=f"$.decorative_interfaces[{interface.id}].feature_id",
                        expected=sorted(eligible),
                        actual=interface.feature_id,
                    )
                )
    if document.modules or document.interfaces:
        from .integrations import validate_contracts

        diagnostics.extend(validate_contracts(record, document.model_dump(mode="json")))
    return diagnostics


def parse_artifact(
    record: Record, name: Artifact, payload: bytes | dict
) -> tuple[Document | None, list[Diagnostic]]:
    model = _MODELS[name]
    try:
        document = (
            model.model_validate_json(payload)
            if isinstance(payload, bytes)
            else model.model_validate(payload)
        )
    except ValidationError as exc:
        return None, [
            artifact_error(
                record,
                name,
                error["msg"],
                path="$"
                + "".join(
                    f"[{key}]" if isinstance(key, int) else f".{key}"
                    for key in error["loc"]
                ),
                expected=error["type"],
                actual=_json_value(error.get("input")),
            )
            for error in exc.errors(include_context=False, include_url=False)[:12]
        ]
    if isinstance(document, Features):
        try:
            issues = _feature_errors(record, document)
        except (OSError, ValueError) as exc:
            issues = [artifact_error(record, "conditions", str(exc), path="$.checks")]
    elif isinstance(document, Plan):
        issues = _plan_errors(record, document)
    else:
        issues = []
    return (document if not issues else None), issues


def _json_value(value: Any) -> Any:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return repr(value)
    return value


def load_artifact(
    record: Record, name: Artifact
) -> tuple[Document | None, list[Diagnostic]]:
    try:
        payload = (record.path / f"{name}.json").read_bytes()
    except OSError as exc:
        return None, [
            artifact_error(
                record, name, f"A readable {name}.json is required.", error=str(exc)
            )
        ]
    return parse_artifact(record, name, payload)


def _response(
    name: Artifact, content: Document | None, diagnostics: list[Diagnostic]
) -> dict:
    return {
        "content": content.model_dump(mode="json", exclude_none=True)
        if content
        else None,
        "schema": _MODELS[name].model_json_schema(),
        "diagnostics": [
            diagnostic.model_dump(mode="json") for diagnostic in diagnostics
        ],
    }


def read_artifact(record: Record, name: Artifact) -> dict:
    path = record.path / f"{name}.json"
    data = (
        _response(name, *load_artifact(record, name))
        if path.exists()
        else _response(name, None, [])
    )
    if name == "features" and record.read_meta().flow:
        try:
            data["conditions"] = read_conditions(record).model_dump(mode="json")
        except (OSError, ValueError) as exc:
            data["diagnostics"].append(
                artifact_error(record, "conditions", str(exc)).model_dump(mode="json")
            )
    return data


def set_artifact(record: Record, name: Artifact, payload: dict) -> dict:
    if not record.read_meta().flow:
        return _response(
            name,
            None,
            [artifact_error(record, name, "Enable flow when creating the record.")],
        )
    if name == "review":
        return _response(
            name,
            None,
            [
                artifact_error(
                    record,
                    name,
                    "Submit the report through the independent review driver.",
                )
            ],
        )
    content, diagnostics = parse_artifact(record, name, payload)
    if content is not None:
        path = record.path / f"{name}.json"
        data = document_bytes(content)
        if file_sha256(path) != sha256(data).hexdigest():
            atomic_write(path, data)
            record.clear_exports()
    return _response(name, content, diagnostics)
