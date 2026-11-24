"""Shared module coordinates and declared mating volumes."""

from __future__ import annotations

import json
from hashlib import sha256
from itertools import combinations
from math import dist
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
from bricklayer.diagnostics import DiagnosticError
from bricklayer.geometry import Pose
from bricklayer.geometry.connections import mating_rule
from bricklayer.ldraw.conversion import pose_from_type1
from bricklayer.ldraw.partdata import get_connector_type
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    ValidationError,
    model_validator,
)

Vec3 = tuple[FiniteFloat, FiniteFloat, FiniteFloat]
Matrix = tuple[Vec3, Vec3, Vec3]
Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
Side = Literal["a", "b"]
SIDE_INDEX = {"a": 0, "b": 1}
EPSILON = 1e-6
ENDPOINT_TOLERANCE_LDU = 0.2
ENDPOINT_AXIS_TOLERANCE = 0.01
# Envelope of just the solid connector, in LDU. The declared rectangular
# cross-section can narrow this envelope but cannot authorize a wide body.
MATING_RADIUS_LDU = {"stud": 6.0, "open_stud": 6.0, "pin": 6.25, "axle": 6.0}
MATING_DEPTH_LDU = {"stud": 4.0, "open_stud": 4.0}


class ModuleError(DiagnosticError):
    """A module operation that needs a source, contract, or snapshot correction."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "module.contract_stale",
        action: Any = None,
        **kwargs,
    ):
        super().__init__(message, code=code, **kwargs)
        self.action = action


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Occupancy(_Model):
    min: Vec3
    max: Vec3

    @model_validator(mode="after")
    def _size(self) -> Occupancy:
        if any(lo >= hi for lo, hi in zip(self.min, self.max)):
            raise ValueError("occupancy min must be below max on every axis")
        return self


class ModuleContract(_Model):
    id: Name
    occupancy: Occupancy
    origin: Vec3
    rotation: Matrix
    palette: tuple[Annotated[int, Field(ge=0, strict=True)], ...]
    part_budget: Annotated[int, Field(gt=0, strict=True)]

    @model_validator(mode="after")
    def _frame(self) -> ModuleContract:
        rotation = np.array(self.rotation)
        if not (
            np.allclose(rotation.T @ rotation, np.eye(3), atol=EPSILON, rtol=0)
            and abs(float(np.linalg.det(rotation)) - 1) <= EPSILON
        ):
            raise ValueError(
                "rotation must be an orthonormal 3×3 rotation with determinant +1"
            )
        if self.id == "main":
            raise ValueError("module ID 'main' is reserved for the root MPD")
        if len(set(self.palette)) != len(self.palette):
            raise ValueError("palette color IDs must be unique")
        return self

    @property
    def pose(self) -> Pose:
        return pose_from_type1(
            (*self.origin, *(v for row in self.rotation for v in row))
        )


class Endpoint(_Model):
    role: Annotated[str, Field(min_length=1)]
    part_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")]
    type: str
    position: Vec3
    axis: Vec3

    @model_validator(mode="after")
    def _axis(self) -> Endpoint:
        if abs(dist(self.axis, (0, 0, 0)) - 1) > EPSILON:
            raise ValueError("endpoint axis must be a unit vector")
        return self


class Intrusion(_Model):
    from_side: Side
    position: Vec3
    direction: Vec3
    half_widths: tuple[
        Annotated[FiniteFloat, Field(gt=0)], Annotated[FiniteFloat, Field(gt=0)]
    ]
    max_depth: Annotated[FiniteFloat, Field(gt=0)]
    tolerance: Annotated[FiniteFloat, Field(ge=0, le=0.25)] = 0.0


class Joint(_Model):
    a: Endpoint
    b: Endpoint
    intrusion: Intrusion | None = None

    def endpoint(self, index: int) -> Endpoint:
        return (self.a, self.b)[index]


class Interface(_Model):
    id: Name
    modules: tuple[Name, Name]
    quantity: Annotated[int, Field(gt=0, strict=True)]
    joints: tuple[Joint, ...]


class Contracts(_Model):
    modules: tuple[ModuleContract, ...] = ()
    interfaces: tuple[Interface, ...] = ()
    shared_colors: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _references(self) -> Contracts:
        modules = {module.id: module for module in self.modules}
        if len(modules) != len(self.modules):
            raise ValueError("module IDs must be unique")
        if len({item.id for item in self.interfaces}) != len(self.interfaces):
            raise ValueError("interface IDs must be unique")
        for a, b in combinations(self.modules, 2):
            if all(
                min(hi_a, hi_b) - max(lo_a, lo_b) > EPSILON
                for lo_a, hi_a, lo_b, hi_b in zip(
                    a.occupancy.min, a.occupancy.max, b.occupancy.min, b.occupancy.max
                )
            ):
                raise ValueError(f"module occupancy interiors overlap: {a.id}, {b.id}")
        assigned = set()
        for interface in self.interfaces:
            a, b = interface.modules
            if a == b or a not in modules or b not in modules:
                raise ValueError(
                    f"{interface.id}: interface must join two distinct declared modules"
                )
            if len(interface.joints) != interface.quantity:
                raise ValueError(
                    f"{interface.id}: quantity must match the number of joints"
                )
            occupancies = (modules[a].occupancy, modules[b].occupancy)
            face = _shared_face(interface.id, *occupancies)
            for joint in interface.joints:
                _check_joint(interface.id, joint, face, occupancies)
                for module_id, endpoint in zip(interface.modules, (joint.a, joint.b)):
                    key = (
                        module_id,
                        endpoint.part_id,
                        endpoint.type,
                        endpoint.position,
                        endpoint.axis,
                    )
                    if key in assigned:
                        raise ValueError(
                            f"{interface.id}: endpoint already allocated to a joint: {key}"
                        )
                    assigned.add(key)
        return self

    def module(self, module_id: str) -> ModuleContract:
        for module in self.modules:
            if module.id == module_id:
                return module
        raise ModuleError(f"Module {module_id!r} is absent from the parent plan.")

    def for_module(self, module_id: str) -> tuple[Interface, ...]:
        return tuple(
            interface for interface in self.interfaces if module_id in interface.modules
        )

    def bound_contract(self, module_id: str) -> bytes:
        module = self.module(module_id)
        return json_bytes(
            {
                "schema_version": 1,
                "module": module.model_dump(mode="json"),
                "shared_colors": sorted(self.shared_colors),
                "interfaces": [
                    interface.model_dump(mode="json")
                    for interface in sorted(
                        self.for_module(module_id), key=lambda item: item.id
                    )
                ],
            }
        )


def parse_contracts(plan: dict) -> Contracts:
    return Contracts.model_validate(
        {
            "modules": plan.get("modules", []),
            "interfaces": plan.get("interfaces", []),
            "shared_colors": plan.get("colors", []),
        }
    )


def validate_contracts(plan: dict) -> None:
    """Validate the module and interface fields in the encompassing flow plan."""
    parse_contracts(plan)


def read_contracts(record_path: Path) -> Contracts:
    path = record_path / "plan.json"
    try:
        return parse_contracts(json.loads(path.read_bytes()))
    except FileNotFoundError:
        return Contracts()
    except (AttributeError, OSError, ValidationError, ValueError) as exc:
        raise ModuleError(f"Could not read module contracts in {path}: {exc}") from exc


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def digest(contents: bytes) -> str:
    return sha256(contents).hexdigest()


def axial_dimension(direction: Vec3) -> int:
    return max(range(3), key=lambda axis: abs(direction[axis]))


def _shared_face(
    name: str, a: Occupancy, b: Occupancy
) -> tuple[int, float, int, Vec3, Vec3]:
    low = tuple(max(a.min[i], b.min[i]) for i in range(3))
    high = tuple(min(a.max[i], b.max[i]) for i in range(3))
    for axis in range(3):
        if abs(low[axis] - high[axis]) > EPSILON:
            continue
        if any(high[i] - low[i] <= EPSILON for i in range(3) if i != axis):
            continue
        sign = 1 if a.max[axis] <= b.min[axis] + EPSILON else -1
        return (
            axis,
            low[axis],
            sign,
            (low[0], low[1], low[2]),
            (high[0], high[1], high[2]),
        )
    raise ValueError(f"{name}: module occupancies must share a face of positive area")


def _check_joint(
    name: str,
    joint: Joint,
    face: tuple[int, float, int, Vec3, Vec3],
    occupancies: tuple[Occupancy, Occupancy],
) -> None:
    axis, seam, sign, low, high = face
    transverse = [i for i in range(3) if i != axis]
    try:
        a_type, b_type = (
            get_connector_type(joint.a.type),
            get_connector_type(joint.b.type),
        )
    except KeyError as exc:
        raise ValueError(f"{name}: unknown connector type {exc}") from exc
    if mating_rule(a_type, b_type) is None:
        raise ValueError(
            f"{name}: incompatible connector types {joint.a.type}, {joint.b.type}"
        )

    def on_face(point: Vec3) -> bool:
        return abs(point[axis] - seam) <= EPSILON and all(
            low[i] - EPSILON <= point[i] <= high[i] + EPSILON for i in transverse
        )

    entry = joint.intrusion
    if entry is None:
        if not all(on_face(endpoint.position) for endpoint in (joint.a, joint.b)):
            raise ValueError(
                f"{name}: endpoints must lie on the shared face without an intrusion"
            )
        if dist(joint.a.position, joint.b.position) > ENDPOINT_TOLERANCE_LDU:
            raise ValueError(
                f"{name}: endpoints must meet at the same point on the shared face"
            )
        return
    source_side = SIDE_INDEX[entry.from_side]
    source = joint.endpoint(source_side)
    source_sign = sign if entry.from_side == "a" else -sign
    direction = tuple(source_sign if i == axis else 0 for i in range(3))
    if dist(entry.direction, direction) > EPSILON or not on_face(entry.position):
        raise ValueError(
            f"{name}: intrusion must start on and follow the normal of the shared face"
        )
    receiving = occupancies[1 - source_side]
    toward_limit = (receiving.max if source_sign > 0 else receiving.min)[axis]
    if entry.max_depth + entry.tolerance > abs(toward_limit - seam) + EPSILON:
        raise ValueError(f"{name}: intrusion depth exceeds receiving occupancy")
    if source.type not in MATING_RADIUS_LDU:
        raise ValueError(
            f"{name}: intrusion requires a stud, pin or axle on the source side"
        )
    if abs(source.axis[axis]) < 1 - ENDPOINT_AXIS_TOLERANCE:
        raise ValueError(
            f"{name}: the source endpoint is not aligned with its intrusion direction"
        )
    for i, half_width in zip(transverse, entry.half_widths):
        reach = half_width + entry.tolerance
        if (
            entry.position[i] - reach < low[i] - EPSILON
            or entry.position[i] + reach > high[i] + EPSILON
        ):
            raise ValueError(
                f"{name}: intrusion cross-section must fit the shared face"
            )
        if any(
            abs(endpoint.position[i] - entry.position[i]) > ENDPOINT_TOLERANCE_LDU
            for endpoint in (joint.a, joint.b)
        ):
            raise ValueError(f"{name}: endpoint and intrusion are not coaxial")
    if any(
        abs(endpoint.position[axis] - seam) > entry.max_depth + entry.tolerance
        for endpoint in (joint.a, joint.b)
    ):
        raise ValueError(f"{name}: endpoint lies beyond the declared interface depth")
