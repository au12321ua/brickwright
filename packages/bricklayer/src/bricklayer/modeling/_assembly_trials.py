"""Checks and diagnostics for a proposed connection in a specific build."""

from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING

from ..diagnostics import DiagnosticError, connector_evidence
from ..geometry.connections import (
    connectors_mate,
    mating_rule,
    same_connector,
)
from ..geometry.spatial import BUILD_TOLERANCE
from ..ldraw.meshes import MeshUnavailable
from ..parts import Element
from .articulation import Joint, MotionRange, MotionReport, record_connection
from .assembly import (
    _SLIDING,
    ConnectionCandidate,
    ConnectionEndpoint,
    ConnectionTrial,
    _occupants,
    _opens_toward,
    _Path,
    _slide_offset,
    _slide_ranges,
)
from .placement import Placement, connect_placement, leaf_paths, path_pose

if TYPE_CHECKING:
    from pydantic import JsonValue

    from ..geometry.connections import JointCoordinates
    from .build import LegoBuild


def trial_connection(
    model: LegoBuild,
    candidate: ConnectionCandidate,
    *,
    samples: int,
    max_samples: int,
    name: str | None = None,
    index: int | None = None,
) -> tuple[ConnectionTrial, tuple[Placement, Joint] | None]:
    """Check one candidate on a scratch model and return its prepared placement."""
    checks = ["endpoints"]

    def failed(error: DiagnosticError) -> tuple[ConnectionTrial, None]:
        return ConnectionTrial(candidate, tuple(checks), (error,)), None

    content = candidate.content
    try:
        model._check_placeable(content)
    except ValueError as error:
        return failed(
            DiagnosticError(
                str(error),
                code="assembly.invalid_content",
                next_action="Attach a separate element or sub-model.",
            )
        )
    parent = _live_endpoint(model, candidate.parent)
    child = _live_endpoint(content, candidate.child)
    if parent is None or child is None:
        return failed(
            DiagnosticError(
                "An endpoint in the candidate is no longer in the build",
                code="connector.stale",
                evidence=_candidate_evidence(model, candidate),
                next_action="Run find_connection_candidates again with the current build.",
            )
        )
    checks.append("mating")
    try:
        placement = connect_placement(
            content,
            connector=child.connector,
            to=parent.connector,
            mounting=candidate.mounting,
            target_rotation=candidate.target_rotation,
            target_position=candidate.target_position,
        )
    except ValueError as error:
        evidence = _candidate_evidence(model, candidate)
        if isinstance(error, DiagnosticError):
            return failed(
                DiagnosticError(
                    str(error),
                    code=error.code,
                    evidence=evidence | error.evidence,
                    next_action=error.next_action,
                )
            )
        return failed(
            DiagnosticError(
                str(error),
                code="connector.unsolved_pose",
                evidence=evidence,
                next_action="Provide a valid target rotation and mounting for this joint.",
            )
        )
    placed_source = child.connector.transformed(placement.pose)
    if not connectors_mate(parent.connector, placed_source):
        return failed(
            DiagnosticError(
                "The fitted pose does not keep the connectors engaged",
                code="connector.unsolved_pose",
                evidence=_candidate_evidence(model, candidate)
                | {
                    "mounted_connector": connector_evidence(placed_source),
                },
                next_action="Adjust mounting or target_position and query the candidate again.",
            )
        )
    if not _opens_toward(
        content,
        placement.pose,
        parent.connector,
        candidate.motion,
        candidate.opening_toward,
        candidate.opening_point,
    ):
        return failed(
            DiagnosticError(
                "The incoming content no longer opens in the requested direction",
                code="connector.opening_direction",
                evidence=_candidate_evidence(model, candidate),
                next_action="Try another mounting or provide a point on the moving content.",
            )
        )
    rule = mating_rule(parent.connector.type, child.connector.type)
    assert rule is not None
    parent_mates = _occupants(model, parent)
    child_mates = () if isinstance(content, Element) else _occupants(content, child)
    if parent_mates or child_mates:
        ranges = _slide_ranges(
            parent.connector, child.connector, parent_mates, child_mates, placement.pose
        )
        offset = _slide_offset(parent.connector, placed_source)
        lower, upper = _requested_offsets(offset, candidate.motion)
        if rule.joint not in _SLIDING or not any(
            lo - BUILD_TOLERANCE <= lower <= upper <= hi + BUILD_TOLERANCE
            for lo, hi in ranges
        ):
            return failed(
                DiagnosticError(
                    "The requested position or travel would reuse an occupied connector",
                    code="connector.occupied",
                    evidence=_candidate_evidence(model, candidate)
                    | {
                        "occupants": [
                            connector_evidence(c) for c in (*parent_mates, *child_mates)
                        ],
                        "slide_ranges": [[lo, hi] for lo, hi in ranges],
                        "requested_offsets": [lower, upper],
                    },
                    next_action="Query current endpoints and use a free endpoint or slide interval.",
                )
            )

    checks.append("collision")
    joint = record_connection(
        model,
        placement,
        child.connector,
        parent.connector,
        None,
        parent_path=parent.path,
        child_path=child.path,
    )
    assert joint is not None
    staged = model.copy()
    staged._place(placement, name, index, joint=joint)
    paths = {
        leaf: path
        for path, leaf in zip(
            leaf_paths(staged.placements), staged.flattened_placements
        )
    }
    try:
        collisions = [
            pair
            for pair in staged.colliding_placements
            if any(paths[p][0] is placement for p in pair)
        ]
    except MeshUnavailable as error:
        return failed(
            DiagnosticError(
                str(error),
                code="catalog.missing_data",
                evidence=_candidate_evidence(model, candidate)
                | {"capability": "collision"},
                next_action="Use lookup_part to find a part with an available collision mesh.",
            )
        )
    if collisions:
        return failed(
            DiagnosticError(
                "The incoming content intersects another placed part",
                code="assembly.collision",
                evidence={
                    "count": len(collisions),
                    "examples": [
                        {"placements": [_path_evidence(staged, paths[p]) for p in pair]}
                        for pair in collisions[:3]
                    ],
                    **_candidate_evidence(model, candidate),
                },
                next_action="Trial another mounting or move the obstructing part.",
            )
        )
    kind = joint.kind
    if candidate.required_kind is not None or candidate.motion is not None:
        checks.append("joint_kind")
    for requested in (
        candidate.required_kind,
        None if candidate.motion is None else candidate.motion.lower.kind,
    ):
        if requested is not None and requested is not kind:
            return failed(
                DiagnosticError(
                    f"The mounted joint is {kind.name}; the candidate requested {requested.name}",
                    code="connector.motion_kind",
                    evidence=_candidate_evidence(model, candidate)
                    | {
                        "expected": requested.name,
                        "actual": kind.name,
                    },
                    next_action="Use a compatible MotionRange or select a different pair.",
                )
            )
    if candidate.motion is not None:
        joint = staged.set_motion(joint, candidate.motion)

    report = None
    diagnostics: tuple[DiagnosticError, ...] = ()
    if any(j.motion is not None for j in staged.flattened_joints):
        checks.append("motion")
        try:
            report = staged.check_motion(samples=samples, max_samples=max_samples)
        except ValueError as error:
            return failed(
                DiagnosticError(
                    str(error),
                    code="motion.unavailable",
                    evidence={"samples": samples, "max_samples": max_samples},
                    next_action="Inspect the current articulated joints and the sampling budget.",
                )
            )
        diagnostics = _motion_diagnostics(staged, report, paths)
    trial = ConnectionTrial(candidate, tuple(checks), diagnostics, kind, report)
    return trial, None if diagnostics else (placement, joint)


def _requested_offsets(
    offset: float, motion: MotionRange | None
) -> tuple[float, float]:
    if motion is None:
        return offset, offset
    return offset + float(getattr(motion.lower, "offset", 0)), offset + float(
        getattr(motion.upper, "offset", 0)
    )


def _live_endpoint(
    content: Element | LegoBuild,
    endpoint: ConnectionEndpoint,
) -> ConnectionEndpoint | None:
    if isinstance(content, Element):
        leaf = content
        part_id = content.ldraw_id
    else:
        leaf = content._connection_leaves.get(endpoint.path)
        if leaf is None:
            return None
        part_id = leaf.content.ldraw_id
    if part_id != endpoint.part_id:
        return None
    if endpoint.connector in leaf.connectors:
        return endpoint
    for connector in leaf.connectors:
        if same_connector(endpoint.connector, connector):
            return ConnectionEndpoint(endpoint.part_id, connector, endpoint.path)
    return None


def _candidate_evidence(
    model: LegoBuild, candidate: ConnectionCandidate
) -> dict[str, JsonValue]:
    return {
        "parent": _endpoint_evidence(model, candidate.parent),
        "child": _endpoint_evidence(candidate.content, candidate.child),
    }


def _endpoint_evidence(
    content: Element | LegoBuild, endpoint: ConnectionEndpoint
) -> dict[str, JsonValue]:
    path = (
        _path_evidence(content, endpoint.path)
        if not isinstance(content, Element) and endpoint.path
        else {}
    )
    return {
        "part_id": endpoint.part_id,
        **path,
        "connector": connector_evidence(endpoint.connector),
    }


def _path_evidence(model: LegoBuild, path: _Path) -> dict[str, JsonValue]:
    labels: list[JsonValue] = []
    current = model
    for placement in path:
        label: str | int | None = current.name_of(placement)
        if label is None:
            try:
                label = current.placements.index(placement)
            except ValueError:
                label = "[removed]"
        labels.append(label)
        current = placement.content
    return {
        "path": labels,
        "part_id": path[-1].content.ldraw_id,
        "position": [float(v) for v in path_pose(path).translation],
    }


def _motion_diagnostics(
    model: LegoBuild,
    report: MotionReport,
    paths: dict[Placement, _Path],
) -> tuple[DiagnosticError, ...]:
    issues = []
    if report.collisions:
        issues.append(
            DiagnosticError(
                "Parts intersect during the sampled motion",
                code="motion.collision",
                evidence={
                    "count": len(report.collisions),
                    "samples_checked": report.samples_checked,
                    "samples": [
                        {
                            "placements": [
                                _path_evidence(model, paths[p])
                                for p in failure.placements
                            ],
                            "positions": _position_evidence(model, failure.positions),
                        }
                        for failure in report.collisions[:3]
                    ],
                },
                next_action="Increase clearance at the reported pose or try a different mounting.",
            )
        )
    if report.disconnections:
        issues.append(
            DiagnosticError(
                "A connection separates during the sampled motion",
                code="motion.disconnected",
                evidence={
                    "count": len(report.disconnections),
                    "samples_checked": report.samples_checked,
                    "samples": [
                        {
                            "joint": _joint_evidence(model, failure.joint),
                            "positions": _position_evidence(model, failure.positions),
                        }
                        for failure in report.disconnections[:3]
                    ],
                },
                next_action="Reduce travel or use connectors that stay engaged at the reported pose.",
            )
        )
    if report.floating:
        issues.append(
            DiagnosticError(
                "Parts lose support during the sampled motion",
                code="motion.floating",
                evidence={
                    "count": len(report.floating),
                    "samples_checked": report.samples_checked,
                    "samples": [
                        {
                            "count": len(failure.placements),
                            "placements": [
                                _path_evidence(model, paths[p])
                                for p in failure.placements[:4]
                            ],
                            "positions": _position_evidence(model, failure.positions),
                        }
                        for failure in report.floating[:3]
                    ],
                },
                next_action="Keep unsupported parts connected to the moving sub-model or its base.",
            )
        )
    return tuple(issues)


def _joint_evidence(model: LegoBuild, joint: Joint) -> dict[str, JsonValue]:
    return {
        "parent": _path_evidence(model, joint.parent.path)
        | {
            "connector": connector_evidence(joint.parent.connector),
        },
        "child": _path_evidence(model, joint.child.path)
        | {
            "connector": connector_evidence(joint.child.connector),
        },
    }


def _position_evidence(
    model: LegoBuild,
    positions: tuple[tuple[Joint, JointCoordinates], ...],
) -> list[JsonValue]:
    return [
        {
            **_joint_evidence(model, joint),
            "kind": coordinates.kind.name.lower(),
            "coordinates": {
                f.name: float(getattr(coordinates, f.name)) for f in fields(coordinates)
            },
        }
        for joint, coordinates in positions
    ]
