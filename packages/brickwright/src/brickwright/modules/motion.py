"""Seal recorded joint ranges alongside the static module MPD."""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from dataclasses import astuple
from typing import Annotated

from bricklayer import Element, LegoBuild
from bricklayer.geometry import (
    Ball,
    Connector,
    Cylindrical,
    Fixed,
    Position,
    Prismatic,
    Revolute,
    Vec3,
)
from bricklayer.geometry.connections import same_connector
from bricklayer.io import LDrawImportWarning, from_ldraw, to_ldraw
from bricklayer.ldraw.conversion import pose_from_type1, pose_to_ldraw
from bricklayer.ldraw.partdata import get_connector_type
from bricklayer.modeling.articulation import (
    Joint,
    JointEndpoint,
    MotionRange,
    remap_joint,
)
from bricklayer.modeling.placement import Placement, leaf_paths
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .contracts import ModuleError, json_bytes
from .contracts import Vec3 as Vector

_COORDINATES = {
    coordinate.__name__: coordinate
    for coordinate in (Fixed, Revolute, Prismatic, Cylindrical, Ball)
}
_NONNEGATIVE = Annotated[int, Field(ge=0)]
_NO_MOTION = json_bytes({})


def _unsupported(message: str) -> ModuleError:
    return ModuleError(
        message,
        code="module.articulation_unsupported",
        next_action="Use standard catalog mating endpoints or keep the articulated assembly in the parent build.",
    )


class _Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leaf: _NONNEGATIVE
    part_id: str
    connection: str
    position: Vector
    axis: Vector
    side: Vector
    length: float
    pose: tuple[float, ...] = Field(min_length=12, max_length=12)

    @classmethod
    def capture(cls, endpoint: JointEndpoint, leaf: int) -> _Frame:
        connector = endpoint.connector
        part = endpoint.path[-1].content
        if not isinstance(part, Element):
            raise _unsupported(
                "A moving joint must reference a catalog part occurrence."
            )
        rotation, translation = pose_to_ldraw(endpoint.frame)
        return cls(
            leaf=leaf,
            part_id=part.ldraw_id,
            connection=connector.type.name,
            position=tuple(connector.position),
            axis=tuple(connector.axis),
            side=tuple(connector.side),
            length=float(connector.length),
            pose=(*translation.tolist(), *rotation.flatten().tolist()),
        )

    def resolve(self, paths: tuple[tuple[Placement, ...], ...]) -> JointEndpoint:
        path = paths[self.leaf]
        part = path[-1].content
        if not isinstance(part, Element) or part.ldraw_id != self.part_id:
            raise _unsupported(
                f"The reread leaf {self.leaf} is not the expected part {self.part_id}."
            )
        expected = Connector(
            get_connector_type(self.connection),
            Position(*self.position),
            Vec3(*self.axis),
            Vec3(*self.side),
            self.length,
        )
        candidates = [
            connector
            for connector in part.connectors
            if same_connector(connector, expected)
        ]
        if len(candidates) != 1:
            raise _unsupported(
                f"Module joint leaf {self.leaf} has no unique standard {self.connection} endpoint."
            )
        return JointEndpoint(path, candidates[0], pose_from_type1(self.pose))


class _Range(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    lower: tuple[float, ...]
    upper: tuple[float, ...]

    def resolve(self) -> MotionRange:
        coordinates = _COORDINATES[self.kind]
        return MotionRange(coordinates(*self.lower), coordinates(*self.upper))


class _Joint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent: _Frame
    child: _Frame
    motion: _Range | None


class _Motion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    joints: tuple[_Joint, ...] = ()

    def resolve(self, build: LegoBuild) -> list[Joint]:
        paths = tuple(leaf_paths(build.placements))
        return [
            Joint(
                item.parent.resolve(paths),
                item.child.resolve(paths),
                item.motion.resolve() if item.motion is not None else None,
            )
            for item in self.joints
        ]


def capture_module_motion(build: LegoBuild) -> dict[str, JsonValue]:
    """Bind declared motion to MPD leaf order and standard catalog connectors."""
    joints = build.flattened_joints
    if not any(joint.motion is not None for joint in joints):
        return {}
    frames = tuple(leaf_paths(build.placements))
    paths = {path: index for index, path in enumerate(frames)}
    if len(paths) != len(frames):
        raise _unsupported("Module motion requires distinct leaf occurrences.")
    records = []
    for joint in joints:
        limits = joint.motion
        declared = (
            None
            if limits is None
            else _Range(
                kind=type(limits.lower).__name__,
                lower=astuple(limits.lower),
                upper=astuple(limits.upper),
            )
        )
        records.append(
            _Joint(
                parent=_Frame.capture(joint.parent, paths[joint.parent.path]),
                child=_Frame.capture(joint.child, paths[joint.child.path]),
                motion=declared,
            )
        )
    artifact = _Motion(joints=tuple(records))
    data = artifact.model_dump(mode="json")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", LDrawImportWarning)
            standard = from_ldraw(to_ldraw(build), ldraw_library="")
    except (ValueError, LDrawImportWarning) as exc:
        raise _unsupported(
            f"The articulated module does not reread with the standard catalog: {exc}"
        ) from exc
    _resolve(standard, json_bytes(data))
    return data


def restore_motion(build: LegoBuild, data: bytes) -> LegoBuild:
    """Reattach a successful child report's joint records to the reread MPD."""
    joints = _resolve(build, data)
    if not joints:
        return build
    return LegoBuild(build.placements, build.names, build.step_markers, joints=joints)


def check_merged_motion(
    build: LegoBuild, artifacts: Mapping[str, bytes]
) -> dict[str, JsonValue] | None:
    """Check the same declared ranges against the whole static placement graph."""
    replacements = {}
    modules = []
    for name, data in artifacts.items():
        placement = build.names.get(name)
        if (
            data == _NO_MOTION
            or placement is None
            or not isinstance(placement.content, LegoBuild)
        ):
            continue
        modules.append(name)
        replacements[placement] = Placement(
            restore_motion(placement.content, data), placement.pose
        )
    if not modules:
        return None

    # Use the sealed ranges even if parent source edited the placed module at
    # runtime. Keep any independently authored joints in the parent.
    paths = {
        path: (replacements.get(path[0], path[0]), *path[1:])
        for path in leaf_paths(build.placements)
    }
    staged = LegoBuild(
        (replacements.get(p, p) for p in build.placements),
        {name: replacements.get(p, p) for name, p in build.names.items()},
        build.step_markers,
        joints=[remap_joint(joint, paths) for joint in build.joints],
    )
    try:
        report = staged.check_motion()
    except ValueError as exc:
        raise _unsupported(
            f"The merged model cannot check the declared module ranges: {exc}"
        ) from exc
    evidence: dict[str, JsonValue] = {
        "modules": modules,
        "samples_checked": report.samples_checked,
    }
    if not report.passed:
        raise ModuleError(
            "A sampled module range collides, loses support, or disengages in the merged model.",
            code="module.motion_failed",
            evidence=evidence
            | {
                "collisions": len(report.collisions),
                "floating": len(report.floating),
                "disconnections": len(report.disconnections),
            },
            next_action="Check moving clearances and support with the adjacent modules assembled.",
        )
    return evidence


def _resolve(build: LegoBuild, data: bytes) -> list[Joint]:
    try:
        return _Motion.model_validate_json(data).resolve(build)
    except ModuleError:
        raise
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise _unsupported(
            f"Cannot restore a standard module joint from the sealed motion data: {exc}"
        ) from exc
