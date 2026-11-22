"""Connection-driven assembly.

Use `find_connection_pairs` to look up complementary parts by intent. Once a
host is placed, `find_connection_candidates` resolves its world endpoints and
proposes mounting poses. Inspect `trial_connections` to see which candidates
are clear. `place_connection` rechecks the chosen candidate before adding it.

```python
import bricklayer as bl

# A panel on the front face.
pair, = bl.find_connection_pairs("side", facing=bl.Direction.FRONT, limit=1)
model = bl.LegoBuild()
host = model.add(bl.Element(pair.parent.part_id, bl.Color.WHITE), name="host")
candidates = model.find_connection_candidates(host, "side", facing=bl.Direction.FRONT)
trials = model.trial_connections(candidates)
panel = model.place_connection(next(trial.candidate for trial in trials if trial.passed))
assert model.is_connected() and model.is_valid()
```
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from functools import cache
from itertools import chain, islice
from typing import TYPE_CHECKING, TypeAlias

from ..colors import Color
from ..diagnostics import DiagnosticError
from ..geometry import (
    Connector,
    ConnectorType,
    Direction,
    JointKind,
    LegoGeometry,
    Mat3,
    Mounting,
    Pose,
    Position,
    Rotation,
    Vec3,
)
from ..geometry.base import _connector_order
from ..geometry.connections import (
    GRID_FAMILIES,
    _reach,
    _slide_limit,
    connectors_mate,
    fit_mounting,
    joint_pose,
    mate_pose,
    mating_rule,
    same_connector,
)
from ..geometry.linalg import dot, is_aligned, is_unit
from ..geometry.spatial import BUILD_TOLERANCE, as_position
from ..ldraw import partdata
from ..parts import Element
from ..parts.mountings import verified_mountings
from ..parts.official import lookup_part, parts_with_connectors
from .articulation import MotionRange, MotionReport
from .placement import Placement, connect_placement, leaf_paths, path_pose

if TYPE_CHECKING:
    from .build import LegoBuild, NameRef

ConnectorQuery: TypeAlias = ConnectorType | str | Iterable[ConnectorType | str]
_Toward: TypeAlias = Position | tuple[float, float, float] | Direction
_Path: TypeAlias = tuple[Placement, ...]
_SlideRanges: TypeAlias = tuple[tuple[float, float], ...]

_SLIDING = frozenset({JointKind.PRISMATIC, JointKind.CYLINDRICAL})
_QUARTER_TURNS = (0, 90, 180, 270)


class AssemblyIntent(StrEnum):
    """The connection requirements used to find candidate parts.

    `side` selects a horizontal stud or antistud on the host. `rotate` selects
    revolute hinges and other non-stud pivots. `slide` selects axial sliding
    rules. `clip`, `ball`, and `pin` select bar/clip, ball/socket, and pin/hole
    interfaces respectively.
    """

    SIDE = "side"
    ROTATE = "rotate"
    SLIDE = "slide"
    CLIP = "clip"
    BALL = "ball"
    PIN = "pin"


@dataclass(frozen=True)
class ConnectionEndpoint:
    """One endpoint on a catalog part or leaf occurrence.

    `connector` is in the querying model's frame (or the part's local frame
    for a catalog part). `path` contains each placement from that model down
    to the leaf and is empty for a loose element.
    """

    part_id: str
    connector: Connector
    path: _Path = ()


@dataclass(frozen=True)
class ConnectionPair:
    """Two catalog endpoints and a seating pose in the host part's frame.

    `joint_kind` is the registered compatibility rule. A cylindrical rule
    can resolve to a prismatic joint when the actual parts limit rotation.
    `verification_scope` describes any catalog checks for the listed mounting.
    """

    parent: ConnectionEndpoint
    child: ConnectionEndpoint
    joint_kind: JointKind
    pose: Pose
    reason: str
    mounting: Mounting = field(default_factory=Mounting)
    verification_scope: str | None = None

    @property
    def requires_trial(self) -> bool:
        """Check collision and movement in the model before committing."""
        return True


@dataclass(frozen=True)
class ConnectionCandidate:
    """A proposed attachment to a particular occurrence in a build.

    `pose` is the fitted pose of the entire incoming content, or `None` if the
    proposed mounting could not be solved. `slide_ranges` are inclusive intervals
    of available offsets from the host endpoint along its axis, in studs.
    Trial the candidate to check the fitted pose and any requested motion.
    """

    parent: ConnectionEndpoint
    child: ConnectionEndpoint
    content: Element | LegoBuild
    joint_kind: JointKind
    mounting: Mounting
    pose: Pose | None
    reason: str
    slide_ranges: _SlideRanges = ()
    target_rotation: Mat3 | Rotation | None = None
    target_position: Position | None = None
    motion: MotionRange | None = None
    required_kind: JointKind | None = None
    opening_toward: Direction | Vec3 | None = None
    opening_point: Position | None = None
    verification_scope: str | None = None


@dataclass(frozen=True)
class ConnectionTrial:
    """The checks attempted for one candidate and any resulting diagnostics."""

    candidate: ConnectionCandidate
    checks: tuple[str, ...]
    diagnostics: tuple[DiagnosticError, ...] = ()
    joint_kind: JointKind | None = None
    motion_report: MotionReport | None = None

    @property
    def passed(self) -> bool:
        """Every attempted check passed."""
        return not self.diagnostics


class AssemblyError(DiagnosticError):
    """A checked placement failed. Inspect `trial` for all checks and diagnostics."""

    def __init__(self, trial: ConnectionTrial) -> None:
        error = trial.diagnostics[0]
        super().__init__(
            str(error),
            code=error.code,
            evidence={**error.evidence, "checks": list(trial.checks)},
            next_action=error.next_action,
        )
        self.trial = trial


def find_connection_pairs(
    intent: AssemblyIntent | str,
    *,
    parent_parts: Iterable[str] | None = None,
    child_parts: Iterable[str] | None = None,
    parent_type: ConnectorQuery | None = None,
    child_type: ConnectorQuery | None = None,
    joint_kind: JointKind | None = None,
    facing: Direction | Vec3 | None = None,
    limit: int = 20,
) -> tuple[ConnectionPair, ...]:
    """Find up to `limit` pairs of compatible catalog endpoints.

    Restrict either side to a set of available part IDs or connector types.
    `facing` selects the parent's connector axis in the parent part's local
    frame. Results start with applicable catalog-verified mounts and then
    favor widely used parts. Each pair gives a pose to consider for trial in
    the current build.
    """
    _check_limit(limit)
    if not limit:
        return ()
    intent = AssemblyIntent(intent)
    parent_types = _catalog_types(parent_type)
    child_types = _catalog_types(child_type)
    rules = _compatible_types(parent_types, child_types, intent, (joint_kind,))
    host_ids = _inventory(parent_parts)
    moving_ids = _inventory(child_parts)
    facing = _facing_vector(facing)
    recorded = (
        pair
        for pair in _registered_pairs(intent)
        if (host_ids is None or pair.parent.part_id in host_ids)
        and (moving_ids is None or pair.child.part_id in moving_ids)
        and pair.child.connector.type in rules.get(pair.parent.connector.type, ())
        and _faces_intent(pair.parent, intent)
        and (facing is None or is_aligned(pair.parent.connector.axis, facing))
    )
    parents = _catalog_parts(frozenset(rules), host_ids, facing)
    catalog = _catalog_pairs(parents, rules, moving_ids, intent)
    return tuple(islice(_distinct_pairs(chain(recorded, catalog)), limit))


def _distinct_pairs(pairs: Iterable[ConnectionPair]) -> Iterator[ConnectionPair]:
    seen: set[tuple[ConnectionEndpoint, ConnectionEndpoint]] = set()
    for pair in pairs:
        endpoints = (pair.parent, pair.child)
        if endpoints not in seen:
            seen.add(endpoints)
            yield pair


def _catalog_pairs(
    parents: Iterable[tuple[ConnectionEndpoint, ...]],
    rules: dict[ConnectorType, dict[ConnectorType, JointKind]],
    child_parts: frozenset[str] | None,
    intent: AssemblyIntent,
) -> Iterator[ConnectionPair]:
    for part_endpoints in parents:
        eligible = tuple(e for e in part_endpoints if _faces_intent(e, intent))
        child_types = frozenset(
            child for e in eligible for child in rules[e.connector.type]
        )
        for children in _catalog_parts(child_types, child_parts):
            for parent in eligible:
                for child in children:
                    kind = rules[parent.connector.type].get(child.connector.type)
                    if kind is None:
                        continue
                    yield ConnectionPair(
                        parent,
                        child,
                        kind,
                        mate_pose(child.connector, parent.connector),
                        _reason(parent, child, kind),
                    )


@cache
def _registered_pairs(intent: AssemblyIntent | None) -> tuple[ConnectionPair, ...]:
    """Prefer verified free motion over a single verified pose when requested."""
    rows = []
    for row in verified_mountings():
        parent, child = (
            ConnectionEndpoint(part_id, partdata.record(part_id).connectors[index])
            for part_id, index in zip(row.part_ids, row.endpoint_indices, strict=True)
        )
        rule = mating_rule(parent.connector.type, child.connector.type)
        assert rule is not None
        if not _matches_intent(
            parent.connector.type, child.connector.type, rule.joint, intent
        ):
            continue
        has_motion = row.motion is not None and row.motion.lower != row.motion.upper
        if intent is AssemblyIntent.SLIDE and row.motion is not None:
            has_motion = any(
                getattr(c, "offset", 0) for c in (row.motion.lower, row.motion.upper)
            )
        forward = ConnectionPair(
            parent,
            child,
            rule.joint,
            row.pose,
            _reason(parent, child, rule.joint),
            row.parameters,
            row.verification_scope,
        )
        parent_rank, child_rank = _part_rank(parent.part_id), _part_rank(child.part_id)
        rows.append((not has_motion, False, parent_rank, child_rank, forward))
        rows.append((True, True, child_rank, parent_rank, _inverse_pair(forward)))
    return tuple(pair for _, _, _, _, pair in sorted(rows, key=lambda row: row[:4]))


def _inverse_pair(pair: ConnectionPair) -> ConnectionPair:
    """Express the same verified fit with the other end anchored."""
    parent, child = pair.child, pair.parent
    pose = pair.pose.inverse
    mounting = fit_mounting(
        pair.joint_kind,
        child.connector,
        parent.connector,
        mate_pose(child.connector, parent.connector),
        target_rotation=pose.rotation,
        target_position=pose.apply(child.connector.position),
    )
    return ConnectionPair(
        parent,
        child,
        pair.joint_kind,
        pose,
        _reason(parent, child, pair.joint_kind),
        mounting,
        f"Inverse of the pose verified with {child.part_id} as host.",
    )


def connection_endpoints(
    model: LegoBuild,
    on: NameRef | Placement | None,
    connector_type: ConnectorQuery | None,
    *,
    facing: Direction | Vec3 | None,
    toward: _Toward | None,
) -> tuple[ConnectionEndpoint, ...]:
    """Implementation of model endpoint lookup."""
    return tuple(
        endpoint
        for endpoint in _model_endpoints(model, on, connector_type, facing, toward)
        if (
            endpoint.connector not in model._mating.connectors
            or _still_reachable(model, endpoint)
        )
    )


def find_connection_candidates(
    model: LegoBuild,
    on: NameRef | Placement | None,
    intent: AssemblyIntent | str | None,
    *,
    content: Element | LegoBuild | None,
    part_ids: Iterable[str] | None,
    color: Color,
    parent_type: ConnectorQuery | None,
    child_type: ConnectorQuery | None,
    joint_kind: JointKind | None,
    facing: Direction | Vec3 | None,
    toward: _Toward | None,
    mountings: Iterable[Mounting] | None,
    target_rotation: Mat3 | Rotation | None,
    target_position: Position | tuple[float, float, float] | None,
    motion: MotionRange | None,
    opening_toward: Direction | Vec3 | None,
    opening_point: Position | tuple[float, float, float] | None,
    limit: int,
) -> tuple[ConnectionCandidate, ...]:
    """Implementation of model candidate lookup."""
    _check_limit(limit)
    if not limit:
        return ()
    intent = None if intent is None else AssemblyIntent(intent)
    if content is not None and part_ids is not None:
        raise ValueError("give either content or part_ids")
    if opening_toward is not None and motion is None:
        raise ValueError("opening_toward requires a motion range")
    if opening_point is not None and opening_toward is None:
        raise ValueError("opening_point requires opening_toward")
    if isinstance(opening_toward, Direction):
        opening_toward = opening_toward.value
    if opening_toward is not None and not is_unit(opening_toward):
        raise ValueError("opening_toward must be a unit vector")

    parents = tuple(
        endpoint
        for endpoint in connection_endpoints(
            model,
            on,
            parent_type,
            facing=facing,
            toward=toward,
        )
        if _faces_intent(endpoint, intent)
    )
    if not parents:
        return ()
    children = None if content is None else _content_endpoints(content, child_type)
    rules = _compatible_types(
        frozenset(e.connector.type for e in parents),
        _catalog_types(child_type)
        if children is None
        else frozenset(e.connector.type for e in children),
        intent,
        (joint_kind, motion.lower.kind if motion is not None else None),
    )
    parent_options = tuple(
        (parent, _occupants(model, parent), rules[parent.connector.type])
        for parent in parents
        if parent.connector.type in rules
    )
    inventory = _inventory(part_ids)
    options = _MountingOptions(
        None if mountings is None else tuple(mountings),
        target_rotation,
        None if target_position is None else as_position(target_position),
        motion,
        joint_kind,
        opening_toward,
        None if opening_point is None else as_position(opening_point),
    )
    # Explicit proximity ranks the actual host endpoints. Within each choice,
    # prefer verified catalog mounts. With no proximity, start with all matches
    # from the verification registry.
    batches = (
        (parent_options,) if toward is None else tuple((p,) for p in parent_options)
    )
    found = _ranked_candidates(
        batches, content, children, inventory, color, options, intent
    )
    return tuple(islice(_distinct_candidates(found), limit))


@dataclass(frozen=True)
class _MountingOptions:
    mountings: tuple[Mounting, ...] | None
    target_rotation: Mat3 | Rotation | None
    target_position: Position | None
    motion: MotionRange | None
    required_kind: JointKind | None
    opening_toward: Vec3 | None
    opening_point: Position | None


_ParentOption: TypeAlias = tuple[
    ConnectionEndpoint, tuple[Connector, ...], dict[ConnectorType, JointKind]
]


def _ranked_candidates(
    batches: Iterable[tuple[_ParentOption, ...]],
    content: Element | LegoBuild | None,
    children: tuple[ConnectionEndpoint, ...] | None,
    inventory: frozenset[str] | None,
    color: Color,
    options: _MountingOptions,
    intent: AssemblyIntent | None,
) -> Iterator[ConnectionCandidate]:
    for parents in batches:
        yield from _registered_candidates(
            parents, content, children, inventory, color, options, intent
        )
        if content is None:
            child_types = frozenset(t for _, _, partners in parents for t in partners)
            groups = (
                (Element(endpoints[0].part_id, color), endpoints)
                for endpoints in _catalog_parts(child_types, inventory)
            )
        else:
            assert children is not None
            groups = ((content, children),)
        yield from _model_candidates(parents, groups, options)


def _registered_candidates(
    parents: tuple[_ParentOption, ...],
    content: Element | LegoBuild | None,
    children: tuple[ConnectionEndpoint, ...] | None,
    inventory: frozenset[str] | None,
    color: Color,
    options: _MountingOptions,
    intent: AssemblyIntent | None,
) -> Iterator[ConnectionCandidate]:
    for parent, parent_mates, partners in parents:
        for pair in _registered_pairs(intent):
            if (
                pair.parent.part_id != parent.part_id
                or pair.child.connector.type not in partners
            ):
                continue
            if not _matches_registered(parent, pair.parent):
                continue
            if content is None:
                if inventory is not None and pair.child.part_id not in inventory:
                    continue
                incoming = Element(pair.child.part_id, color)
                source_endpoints = (pair.child,)
            else:
                incoming = content
                assert children is not None
                source_endpoints = tuple(
                    c for c in children if _matches_registered(c, pair.child)
                )
            for child in source_endpoints:
                child_mates = (
                    () if isinstance(incoming, Element) else _occupants(incoming, child)
                )
                if (
                    options.target_position is None
                    and options.target_rotation is None
                    and options.mountings is None
                ):
                    yield from _mounting_candidates(
                        incoming,
                        parent,
                        child,
                        pair.joint_kind,
                        parent_mates,
                        child_mates,
                        replace(options, mountings=(pair.mounting,)),
                        verification_scope=pair.verification_scope,
                    )
                else:
                    yield from _mounting_candidates(
                        incoming,
                        parent,
                        child,
                        pair.joint_kind,
                        parent_mates,
                        child_mates,
                        options,
                    )


def _matches_registered(
    placed: ConnectionEndpoint, catalog: ConnectionEndpoint
) -> bool:
    return placed.part_id == catalog.part_id and same_connector(
        placed.connector, catalog.connector.transformed(path_pose(placed.path))
    )


def _distinct_candidates(
    candidates: Iterable[ConnectionCandidate],
) -> Iterator[ConnectionCandidate]:
    seen = set()
    for candidate in candidates:
        key = (
            candidate.parent,
            candidate.child,
            candidate.mounting if candidate.pose is None else candidate.pose,
        )
        if key not in seen:
            seen.add(key)
            yield candidate


def _model_candidates(
    parents: tuple[_ParentOption, ...],
    groups: Iterable[tuple[Element | LegoBuild, tuple[ConnectionEndpoint, ...]]],
    options: _MountingOptions,
) -> Iterator[ConnectionCandidate]:
    for content, children in groups:
        source_options = [
            (child, () if isinstance(content, Element) else _occupants(content, child))
            for child in children
        ]
        for parent, parent_mates, partners in parents:
            for child, child_mates in source_options:
                kind = partners.get(child.connector.type)
                if kind is None:
                    continue
                yield from _mounting_candidates(
                    content, parent, child, kind, parent_mates, child_mates, options
                )


def _mounting_candidates(
    content: Element | LegoBuild,
    parent: ConnectionEndpoint,
    child: ConnectionEndpoint,
    kind: JointKind,
    parent_mates: tuple[Connector, ...],
    child_mates: tuple[Connector, ...],
    options: _MountingOptions,
    *,
    verification_scope: str | None = None,
) -> Iterator[ConnectionCandidate]:
    target, source = parent.connector, child.connector
    ranges = _slide_ranges(target, source, parent_mates, child_mates)
    if options.mountings is None:
        if kind in _SLIDING and options.target_position is None:
            offsets = tuple(
                0 if lo <= 0 <= hi else (lo + hi) / 2
                for lo, hi in _travel_centers(ranges, options.motion)
            )
        else:
            offsets = (0,)
        angles = (
            _QUARTER_TURNS
            if options.target_rotation is None
            and kind
            in {
                JointKind.REVOLUTE,
                JointKind.CYLINDRICAL,
                JointKind.BALL,
            }
            else (0,)
        )
        mountings = tuple(
            Mounting(angle, offset) for offset in offsets for angle in angles
        )
    else:
        mountings = options.mountings
    seen: set[Pose] = set()
    for mounting in mountings:
        try:
            placement = connect_placement(
                content,
                connector=source,
                to=target,
                mounting=mounting,
                target_rotation=options.target_rotation,
                target_position=options.target_position,
            )
        except ValueError:
            pose = None
        else:
            pose = placement.pose
            if pose in seen or not _opens_toward(
                content,
                pose,
                target,
                options.motion,
                options.opening_toward,
                options.opening_point,
            ):
                continue
            seen.add(pose)
        slide_ranges = _slide_ranges(target, source, parent_mates, child_mates, pose)
        if verification_scope is not None and kind in _SLIDING and pose is not None:
            offset = _slide_offset(target, source.transformed(pose))
            if not any(
                lo - BUILD_TOLERANCE <= offset <= hi + BUILD_TOLERANCE
                for lo, hi in _travel_centers(slide_ranges, options.motion)
            ):
                continue
        yield ConnectionCandidate(
            parent,
            child,
            content,
            kind,
            mounting,
            pose,
            _reason(parent, child, kind),
            slide_ranges,
            options.target_rotation,
            options.target_position,
            options.motion,
            options.required_kind,
            options.opening_toward,
            options.opening_point,
            verification_scope,
        )


def _catalog_types(query: ConnectorQuery | None) -> frozenset[ConnectorType]:
    if query is None:
        return frozenset(partdata.all_connector_types().values())
    return partdata.resolve_connector_types(query)


def _compatible_types(
    parents: frozenset[ConnectorType],
    children: frozenset[ConnectorType],
    intent: AssemblyIntent | None,
    kinds: tuple[JointKind | None, ...],
) -> dict[ConnectorType, dict[ConnectorType, JointKind]]:
    result: dict[ConnectorType, dict[ConnectorType, JointKind]] = {}
    for parent in parents:
        for child in children:
            rule = mating_rule(parent, child)
            if rule is None or not _matches_intent(parent, child, rule.joint, intent):
                continue
            # The solver classifies a keyed cylindrical interface from its actual fit.
            possible = (
                (rule.joint, JointKind.PRISMATIC)
                if rule.joint is JointKind.CYLINDRICAL
                else (rule.joint,)
            )
            if all(k is None or k in possible for k in kinds):
                result.setdefault(parent, {})[child] = rule.joint
    return result


def _matches_intent(
    parent: ConnectorType,
    child: ConnectorType,
    kind: JointKind,
    intent: AssemblyIntent | None,
) -> bool:
    match intent:
        case AssemblyIntent.SIDE:
            return parent.family in GRID_FAMILIES and child.family in GRID_FAMILIES
        case AssemblyIntent.ROTATE:
            return kind is JointKind.REVOLUTE and parent.family not in GRID_FAMILIES
        case AssemblyIntent.SLIDE:
            return kind in _SLIDING
        case AssemblyIntent.CLIP:
            return {parent.name, child.name} == {"bar", "clip"}
        case AssemblyIntent.BALL:
            return kind is JointKind.BALL
        case AssemblyIntent.PIN:
            return {parent.name, child.name} == {"pin", "pin_hole"}
        case None:
            return True


def _faces_intent(endpoint: ConnectionEndpoint, intent: AssemblyIntent | None) -> bool:
    return (
        intent is not AssemblyIntent.SIDE
        or abs(endpoint.connector.axis.z) <= BUILD_TOLERANCE
    )


def _reason(
    parent: ConnectionEndpoint, child: ConnectionEndpoint, kind: JointKind
) -> str:
    return f"{parent.connector.type.name} mates with {child.connector.type.name} under a {kind.name.lower()} rule"


def _part_rank(part_id: str) -> tuple[int, str]:
    return -partdata.popularity(part_id), part_id


def _inventory(parts: Iterable[str] | None) -> frozenset[str] | None:
    if parts is None:
        return None
    parts = (parts,) if isinstance(parts, str) else parts
    return frozenset(
        part_id.strip().casefold().removesuffix(".dat") for part_id in parts
    )


def _facing_vector(facing: Direction | Vec3 | None) -> Vec3 | None:
    if isinstance(facing, Direction):
        facing = facing.value
    if facing is not None and not is_unit(facing):
        raise ValueError(f"facing must be a unit vector, got {facing!r}")
    return facing


def _catalog_parts(
    types: frozenset[ConnectorType],
    inventory: frozenset[str] | None,
    facing: Vec3 | None = None,
) -> Iterator[tuple[ConnectionEndpoint, ...]]:
    if not types:
        return
    ids = (
        parts_with_connectors(types)
        if inventory is None
        else sorted(inventory, key=_part_rank)
    )
    for part_id in ids:
        facts = lookup_part(part_id)
        if not facts.constructible.available or not facts.collision.available:
            problem = (
                facts.constructible
                if not facts.constructible.available
                else facts.collision
            )
            if inventory is not None:
                raise DiagnosticError(
                    f"{part_id}: {problem.reason}",
                    code="catalog.missing_data"
                    if facts.in_version
                    else "catalog.unknown_part",
                    evidence={
                        "part_id": part_id,
                        "version": facts.version,
                        "reason": problem.reason_code,
                    },
                    next_action="Use lookup_part to inspect available modeling data and related IDs.",
                )
            continue
        element = Element(part_id, Color.WHITE)
        endpoints = tuple(
            ConnectionEndpoint(part_id, c)
            for c in _ordered_connectors(element, types, facing)
        )
        if endpoints:
            yield endpoints


def _model_endpoints(
    model: LegoBuild,
    on: NameRef | Placement | None,
    connector_type: ConnectorQuery | None,
    facing: Direction | Vec3 | None,
    toward: _Toward | None,
) -> list[ConnectionEndpoint]:
    leaves = model._connection_leaves
    target = None if on is None else model._reference(on)
    if target is None:
        paths = leaves
    elif target in model.placements:
        paths = {path: leaf for path, leaf in leaves.items() if path[0] is target}
    else:
        paths = {
            path: leaf
            for path, leaf in leaves.items()
            if leaf is target
            or any(
                _composed_path(model, path[:i]) is target for i in range(2, len(path))
            )
        }
        if not paths:
            raise DiagnosticError(
                "Target placement is no longer in this build",
                code="connector.stale",
                next_action="Select a current placement or a name path in this build.",
            )
    types = (
        None
        if connector_type is None
        else partdata.resolve_connector_types(connector_type)
    )
    result = [
        ConnectionEndpoint(leaf.content.ldraw_id, connector, path)
        for path, leaf in paths.items()
        for connector in _ordered_connectors(leaf, types, facing)
    ]
    if isinstance(toward, Direction):
        result.sort(
            key=lambda endpoint: (
                -dot(endpoint.connector.position.to_stud_units(), toward.value)
            )
        )
    elif toward is not None:
        point = as_position(toward)
        result.sort(
            key=lambda endpoint: (
                (endpoint.connector.position - point).to_stud_units().norm()
            )
        )
    return result


def _composed_path(model: LegoBuild, path: _Path) -> Placement:
    outer, *inner = path
    if not inner:
        return outer
    return model._compose(outer, _composed_path(outer.content, tuple(inner)))


def _content_endpoints(
    content: Element | LegoBuild,
    connector_type: ConnectorQuery | None,
) -> tuple[ConnectionEndpoint, ...]:
    if isinstance(content, Element):
        return tuple(
            ConnectionEndpoint(content.ldraw_id, c)
            for c in _ordered_connectors(content, connector_type)
        )
    return connection_endpoints(content, None, connector_type, facing=None, toward=None)


def _ordered_connectors(
    geometry: LegoGeometry,
    connector_type: ConnectorQuery | None,
    facing: Direction | Vec3 | None = None,
) -> list[Connector]:
    """Find connectors in stable order and collapse duplicate annotations."""
    points = {}
    for connector in geometry.find_connectors(connector_type, facing=facing):
        points.setdefault((connector.type, _connector_order(connector)), connector)
    return sorted(points.values(), key=lambda c: (_connector_order(c), c.type.name))


def connection_leaves(model: LegoBuild) -> dict[_Path, Placement[Element]]:
    """Index unambiguous leaf paths and their placements in the model frame."""
    paths = tuple(leaf_paths(model.placements))
    counts = Counter(paths)
    return {
        path: leaf
        for path, leaf in zip(paths, model.flattened_placements)
        if counts[path] == 1
    }


def _occupants(model: LegoBuild, endpoint: ConnectionEndpoint) -> tuple[Connector, ...]:
    if endpoint.connector not in model._mating.connectors:
        return ()
    index = model.flattened_placements.index(model._connection_leaves[endpoint.path])
    neighbors = (
        b if a == index else a for a, b in model._mating.pairs if index in (a, b)
    )
    return tuple(
        c
        for i in neighbors
        for c in model.flattened_placements[i].find_connectors()
        if connectors_mate(c, endpoint.connector)
    )


def _still_reachable(model: LegoBuild, endpoint: ConnectionEndpoint) -> bool:
    target = endpoint.connector
    mates = _occupants(model, endpoint)
    if any(
        (rule := mating_rule(target.type, mate.type)) is not None
        and rule.joint not in _SLIDING
        for mate in mates
    ):
        return False
    # Use the minimum counterpart reach here. Each actual candidate gets its
    # own fit and usable intervals after both endpoints are known.
    return bool(
        _free_intervals(
            _slide_limit(0, target.length),
            [
                (_slide_offset(target, mate), _reach(mate.length) + _reach(0))
                for mate in mates
            ],
        )
    )


def _slide_ranges(
    parent: Connector,
    child: Connector,
    parent_mates: tuple[Connector, ...],
    child_mates: tuple[Connector, ...],
    pose: Pose | None = None,
) -> _SlideRanges:
    rule = mating_rule(parent.type, child.type)
    if rule is None or rule.joint not in _SLIDING:
        return ()
    sign = 1 if pose is None else dot(pose.rotation @ child.axis, parent.axis)
    blocks = [
        (_slide_offset(parent, mate), _reach(mate.length) + _reach(child.length))
        for mate in parent_mates
    ]
    blocks.extend(
        (
            -sign * _slide_offset(child, mate),
            _reach(mate.length) + _reach(parent.length),
        )
        for mate in child_mates
    )
    return _free_intervals(_slide_limit(child.length, parent.length), blocks)


def _free_intervals(reach: float, blocks: list[tuple[float, float]]) -> _SlideRanges:
    """Subtract occupied intervals from the allowed centers of the moving connector."""
    ranges = [(-reach, reach)]
    for offset, radius in sorted(blocks):
        low, high = offset - radius, offset + radius
        available = []
        for lo, hi in ranges:
            if lo <= low:
                available.append((lo, min(low, hi)))
            if high <= hi:
                available.append((max(lo, high), hi))
        ranges = available
    return tuple(ranges)


def _slide_offset(parent: Connector, other: Connector) -> float:
    return float(dot((other.position - parent.position).to_stud_units(), parent.axis))


def _travel_centers(ranges: _SlideRanges, motion: MotionRange | None) -> _SlideRanges:
    """Seat positions that leave room for the requested motion on each side."""
    if motion is None:
        return ranges
    lower = float(getattr(motion.lower, "offset", 0))
    upper = float(getattr(motion.upper, "offset", 0))
    return tuple(
        (lo - lower, hi - upper) for lo, hi in ranges if lo - lower <= hi - upper
    )


def _opens_toward(
    content: Element | LegoBuild,
    pose: Pose,
    target: Connector,
    motion: MotionRange | None,
    direction: Direction | Vec3 | None,
    opening_point: Position | None,
) -> bool:
    if direction is None:
        return True
    assert motion is not None
    coordinates = motion.upper
    if all(not getattr(coordinates, f.name) for f in fields(coordinates)):
        coordinates = motion.lower
    frame = Pose(target.frame, target.position)
    delta = frame.compose(joint_pose(coordinates)).compose(frame.inverse)
    point = pose.apply(content.center if opening_point is None else opening_point)
    direction = direction.value if isinstance(direction, Direction) else direction
    return (
        dot((delta.apply(point) - point).to_stud_units(), direction) > BUILD_TOLERANCE
    )


def _check_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")


__all__ = [
    "AssemblyError",
    "AssemblyIntent",
    "ConnectionCandidate",
    "ConnectionEndpoint",
    "ConnectionPair",
    "ConnectionTrial",
    "find_connection_pairs",
]
