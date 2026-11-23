"""JSON evidence for candidate lookup and bounded trial batches."""

from __future__ import annotations

from collections import Counter
from math import acos, degrees
from typing import TYPE_CHECKING

from ..diagnostics import connector_evidence
from ..geometry import Rotation
from ..parts import Element
from .placement import path_pose

if TYPE_CHECKING:
    from pydantic import JsonValue

    from ..geometry import Pose
    from .assembly import (
        ConnectionCandidate,
        ConnectionEndpoint,
        ConnectionTrial,
        _Path,
    )
    from .build import LegoBuild

_EXAMPLES = 8
_TRIAL_EXAMPLES = 3


def endpoint_evidence(
    content: Element | LegoBuild, endpoint: ConnectionEndpoint
) -> dict[str, JsonValue]:
    pose = path_pose(endpoint.path)
    occurrence = (
        path_evidence(content, endpoint.path)
        if not isinstance(content, Element) and endpoint.path
        else {}
    )
    return {
        "part_id": endpoint.part_id,
        **occurrence,
        "connector": connector_evidence(endpoint.connector),
        "local_connector": connector_evidence(
            endpoint.connector.transformed(pose.inverse)
        ),
    }


def path_evidence(model: LegoBuild, path: _Path) -> dict[str, JsonValue]:
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


def endpoint_examples(
    content: Element | LegoBuild, endpoints: list[ConnectionEndpoint]
) -> dict[str, JsonValue]:
    # Show connector types before repeating equivalent types on the same part.
    first_by_type = {}
    for endpoint in endpoints:
        first_by_type.setdefault(endpoint.connector.type, endpoint)
    examples = dict.fromkeys((*first_by_type.values(), *endpoints))
    return {
        "count": len(endpoints),
        "examples": [endpoint_evidence(content, e) for e in list(examples)[:_EXAMPLES]],
    }


def fitted_evidence(candidate: ConnectionCandidate, pose: Pose) -> dict[str, JsonValue]:
    connector = pose.apply(candidate.child.connector.position)
    target_rotation = candidate.target_rotation
    if isinstance(target_rotation, Rotation):
        target_rotation = target_rotation.matrix
    error: dict[str, JsonValue] = {}
    if target_rotation is not None:
        # The inner product of the two rotations is the trace of their relative rotation.
        trace = sum(
            a * b for a, b in zip(pose.rotation.flatten(), target_rotation.flatten())
        )
        error["rotation_degrees"] = degrees(acos(max(-1.0, min(1.0, (trace - 1) / 2))))
    if candidate.target_position is not None:
        delta = (connector - candidate.target_position).to_stud_units()
        error["connector_offset_studs"] = [float(v) for v in delta]
        error["connector_distance_studs"] = delta.norm()
    return {
        "fitted_pose": {
            "translation": [float(v) for v in pose.translation],
            "rotation": list(pose.rotation.flatten()),
            "connector_position": [float(v) for v in connector],
        },
        "target_error": error,
    }


def record_trial_batch(
    evidence: dict[str, JsonValue],
    trials: tuple[ConnectionTrial, ...],
    *,
    offered: int | None,
    limit: int,
    samples: int,
    max_samples: int,
) -> None:
    failures = [
        (i, trial) for i, trial in enumerate(trials, start=1) if not trial.passed
    ]
    # Each reason represents a rejected candidate even if several reasons applied.
    counts = Counter(
        code for _, trial in failures for code in {d.code for d in trial.diagnostics}
    )
    passed = len(trials) - len(failures)
    evidence.clear()
    evidence.update(
        stage="trial_connections",
        limit=limit,
        offered=offered,
        checked=len(trials),
        passed=passed,
        samples=samples,
        max_samples=max_samples,
        failure_counts=dict(sorted(counts.items())),
        examples=[
            {
                "rank": rank,
                "checks": list(trial.checks),
                "diagnostics": [
                    {
                        "code": error.code,
                        "message": str(error),
                        "evidence": error.evidence,
                        "next_action": error.next_action,
                    }
                    for error in trial.diagnostics
                ],
            }
            for rank, trial in failures[:_TRIAL_EXAMPLES]
        ],
    )
    if not trials:
        evidence.update(
            code="assembly.no_trials",
            message="No candidate was trialed.",
            next_action=(
                "Use a positive trial limit."
                if limit == 0
                else "Inspect the evidence from find_connection_candidates for the empty input."
            ),
        )
    elif counts["assembly.collision"] == len(trials):
        evidence.update(
            code="assembly.all_colliding",
            message="Every checked candidate collided.",
            next_action=(
                "Repeat trial_connections with an individual candidate from these ranks. "
                "Check the two endpoints and collision data with lookup_part and inspect "
                "the reported interfering placements before changing the mounting."
            ),
        )
    elif not passed:
        evidence.update(
            code="assembly.no_trial_passed",
            message="No checked candidate passed.",
            next_action="Inspect the individual trial diagnostics before retrying or changing the query.",
        )
