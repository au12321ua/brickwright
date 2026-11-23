"""Check the declared interface instances and the actual mesh beyond each face."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from math import dist
from typing import TYPE_CHECKING

import numpy as np
from bricklayer.geometry.connections import Connector, mated
from bricklayer.ldraw.conversion import LDRAW_TO_STUD, LDrawVec
from pydantic import JsonValue

from ..tools.diagnostics import Diagnostic
from .contracts import (
    ENDPOINT_AXIS_TOLERANCE,
    ENDPOINT_TOLERANCE_LDU,
    EPSILON,
    MATING_DEPTH_LDU,
    MATING_RADIUS_LDU,
    SIDE_INDEX,
    Endpoint,
    Interface,
    Intrusion,
    ModuleContract,
    Vec3,
    axial_dimension,
)

if TYPE_CHECKING:
    from bricklayer import Element, LegoBuild
    from bricklayer.modeling.placement import Placement

Box = tuple[Vec3, Vec3]
Polygon = list[np.ndarray]


@dataclass(frozen=True)
class Point:
    index: int
    leaf: Placement[Element]
    connector: Connector

    @property
    def position(self) -> Vec3:
        position = LDrawVec.from_position(self.connector.position)
        return position.x, position.y, position.z

    @property
    def axis(self) -> Vec3:
        axis = LDRAW_TO_STUD.transpose @ self.connector.axis
        return float(axis.x), float(axis.y), float(axis.z)

    def evidence(self, role: str) -> dict[str, JsonValue]:
        return {
            "role": role,
            "instance_index": self.index,
            "part_id": self.leaf.content.ldraw_id,
            "type": self.connector.type.name,
            "position_ldu": list(self.position),
            "axis": list(self.axis),
        }


def interface_points(leaves: Sequence[Placement[Element]]) -> dict[str, list[Point]]:
    points: dict[str, list[Point]] = defaultdict(list)
    for index, leaf in enumerate(leaves):
        for connector in leaf.connectors:
            points[leaf.content.ldraw_id].append(Point(index, leaf, connector))
    return points


def match_endpoint(
    points: dict[str, list[Point]],
    endpoint: Endpoint,
    occupied: frozenset[Connector] = frozenset(),
) -> tuple[Point | None, dict[str, JsonValue]]:
    candidates = points.get(endpoint.part_id, [])
    ranked = sorted(
        (
            (dist(p.position, endpoint.position), dist(p.axis, endpoint.axis), p)
            for p in candidates
            if p.connector.type.name == endpoint.type
        ),
        key=lambda entry: (entry[0], entry[1], entry[2].index),
    )
    available = [
        point
        for delta, alignment, point in ranked
        if delta <= ENDPOINT_TOLERANCE_LDU
        and alignment <= ENDPOINT_AXIS_TOLERANCE
        and point.connector not in occupied
    ]
    evidence: dict[str, JsonValue] = {
        "expected": endpoint.model_dump(mode="json"),
        "matching_available": len(available),
    }
    if ranked:
        delta, alignment, nearest = ranked[0]
        evidence["nearest"] = nearest.evidence(endpoint.role) | {
            "position_error_ldu": delta,
            "axis_error": alignment,
            "occupied": nearest.connector in occupied,
        }
    elif candidates:
        nearest = min(
            candidates, key=lambda point: dist(point.position, endpoint.position)
        )
        evidence["nearest"] = nearest.evidence(endpoint.role) | {
            "position_error_ldu": dist(nearest.position, endpoint.position),
            "axis_error": dist(nearest.axis, endpoint.axis),
            "occupied": nearest.connector in occupied,
        }
    return (available[0] if len(available) == 1 else None), evidence


def self_check(
    build: LegoBuild,
    module: ModuleContract,
    interfaces: Sequence[Interface],
) -> list[Diagnostic]:
    leaves = [leaf.transformed(module.pose) for leaf in build.flattened_placements]
    groups = [leaf.connectors for leaf in leaves]
    occupied = mated(groups).connectors
    points = interface_points(leaves)
    allowances: dict[int, list[tuple[str, Box]]] = defaultdict(list)
    diagnostics = []
    used = set()
    for interface in interfaces:
        side = interface.modules.index(module.id)
        for number, joint in enumerate(interface.joints, start=1):
            endpoint = joint.endpoint(side)
            point, evidence = match_endpoint(points, endpoint, occupied)
            if point is None or point.connector in used:
                diagnostics.append(
                    Diagnostic(
                        code="module.interface_missing",
                        stage="module_validate",
                        message=f"{module.id} needs one free {endpoint.type} for {interface.id}/{number}.",
                        evidence=evidence
                        | {
                            "module": module.id,
                            "interface": interface.id,
                            "joint": number,
                        },
                        next_action="Install the specified part and expose its endpoint at the declared position and direction.",
                    )
                )
                continue
            used.add(point.connector)
            entry = joint.intrusion
            if entry is not None and SIDE_INDEX[entry.from_side] == side:
                allowances[point.index].append(
                    (
                        interface.id,
                        _intrusion_box(entry, point),
                    )
                )
    if not leaves or len(leaves) > module.part_budget:
        diagnostics.append(
            Diagnostic(
                code="module.part_budget",
                stage="module_validate",
                message=f"{module.id} must contain at least one part and meet its part budget.",
                evidence={
                    "module": module.id,
                    "parts": len(leaves),
                    "budget": module.part_budget,
                },
            )
        )
    palette = set(module.palette)
    unlisted = (
        sorted({leaf.content.color.ldraw_id for leaf in leaves} - palette)
        if palette
        else []
    )
    if unlisted:
        diagnostics.append(
            Diagnostic(
                code="module.palette",
                stage="module_validate",
                message=f"{module.id} contains colors absent from its palette.",
                evidence={"module": module.id, "unlisted_colors": unlisted},
            )
        )
    occupied_box = module.occupancy.min, module.occupancy.max
    for index, leaf in enumerate(leaves):
        diagnostics.extend(
            _check_occupancy(leaf, index, module.id, occupied_box, allowances[index])
        )
    return diagnostics


def _intrusion_box(entry: Intrusion, point: Point) -> Box:
    axis = axial_dimension(entry.direction)
    low, high = list(entry.position), list(entry.position)
    transverse = [i for i in range(3) if i != axis]
    connector_type = point.connector.type.name
    radius = MATING_RADIUS_LDU[connector_type]
    center = point.position
    for i, half in zip(transverse, entry.half_widths):
        low[i] = max(entry.position[i] - half, center[i] - radius) - entry.tolerance
        high[i] = min(entry.position[i] + half, center[i] + radius) + entry.tolerance
    sign = entry.direction[axis]
    geometry_depth = MATING_DEPTH_LDU.get(
        connector_type,
        sign * (center[axis] - entry.position[axis])
        + 20 * float(point.connector.length),
    )
    depth = min(entry.max_depth, max(0, geometry_depth))
    end = entry.position[axis] + sign * (depth + entry.tolerance)
    low[axis], high[axis] = sorted((entry.position[axis], end))
    return (low[0], low[1], low[2]), (high[0], high[1], high[2])


def _check_occupancy(
    leaf: Placement[Element],
    index: int,
    module_id: str,
    occupancy: Box,
    allowances: Sequence[tuple[str, Box]],
) -> list[Diagnostic]:
    body = leaf.collision_body
    mesh = body.collider.mesh
    half = (body.collider.hi - body.collider.lo) / 2
    center = (
        body.rotation @ ((body.collider.hi + body.collider.lo) / 2) + body.translation
    )
    reach = np.abs(body.rotation) @ half
    low, high = occupancy
    if np.all(center - reach >= np.array(low) - EPSILON) and np.all(
        center + reach <= np.array(high) + EPSILON
    ):
        return []
    vertices = np.asarray(mesh.vertices) @ body.rotation.T + body.translation
    diagnostics = []
    for axis in range(3):
        for sign, limit in ((-1, low[axis]), (1, high[axis])):
            distances = sign * (vertices[:, axis] - limit)
            if float(np.max(distances)) <= EPSILON:
                continue
            candidate_faces = np.asarray(mesh.faces)[
                np.any(distances[mesh.faces] > EPSILON, axis=1)
            ]
            beyond = 0.0
            for face in candidate_faces:
                _, outside = _split(list(vertices[face]), axis, limit, sign)
                remaining = [outside]
                for _, box in allowances:
                    remaining = [
                        piece
                        for polygon in remaining
                        for piece in _subtract_box(polygon, box)
                    ]
                    if not remaining:
                        break
                for polygon in remaining:
                    if len(polygon) >= 3:
                        beyond = max(
                            beyond, *(sign * (float(p[axis]) - limit) for p in polygon)
                        )
            if beyond > EPSILON:
                direction = [sign if i == axis else 0 for i in range(3)]
                diagnostics.append(
                    Diagnostic(
                        code="module.outside_occupancy",
                        stage="module_validate",
                        message=f"{module_id} part {leaf.content.ldraw_id} extends beyond its permitted occupancy.",
                        evidence={
                            "module": module_id,
                            "part_id": leaf.content.ldraw_id,
                            "instance_index": index,
                            "interfaces": sorted({name for name, _ in allowances}),
                            "direction": direction,
                            "excess_ldu": round(beyond, 6),
                            "instance_translation_ldu": list(
                                LDrawVec.from_position(leaf.pose.translation)
                            ),
                        },
                        next_action="Move the part into its occupancy or use a declared mating endpoint at the crossing.",
                    )
                )
    return diagnostics


def _subtract_box(polygon: Polygon, box: Box) -> list[Polygon]:
    """Partition a convex surface polygon and retain its pieces outside a box."""
    outside = []
    for axis in range(3):
        for sign, limit in ((-1, box[0][axis]), (1, box[1][axis])):
            polygon, piece = _split(polygon, axis, limit, sign)
            if len(piece) >= 3:
                outside.append(piece)
            if len(polygon) < 3:
                return outside
    return outside


def _split(
    polygon: Polygon, axis: int, limit: float, sign: int
) -> tuple[Polygon, Polygon]:
    """Split a triangle fragment at a limit with a closed inner half-space."""
    inside, outside = [], []
    if not polygon:
        return inside, outside
    previous = polygon[-1]
    prior = sign * (float(previous[axis]) - limit) - EPSILON
    for current in polygon:
        distance = sign * (float(current[axis]) - limit) - EPSILON
        if (distance > 0) != (prior > 0):
            intersection = previous + (current - previous) * (
                prior / (prior - distance)
            )
            inside.append(intersection)
            outside.append(intersection)
        (outside if distance > 0 else inside).append(current)
        previous, prior = current, distance
    return inside, outside
