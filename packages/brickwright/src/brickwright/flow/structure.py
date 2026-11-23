"""Compare native and clean-catalog geometry for a single exported build."""

from __future__ import annotations

import warnings
from functools import cache
from hashlib import sha256
from importlib.metadata import version
from importlib.resources import files
from typing import Any, Literal

from bricklayer import LegoBuild, Placement
from bricklayer.diagnostics import connector_evidence
from bricklayer.geometry.connections import mating_rule
from bricklayer.io.reader import LDrawImportWarning, from_ldraw
from bricklayer.ldraw import partdata
from bricklayer.ldraw.conversion import pose_to_ldraw
from bricklayer.parts import Element
from bricklayer.parts.official import OFFICIAL_PARTS_VERSION
from pydantic import BaseModel, Field

from brickwright.tools.diagnostics import Diagnostic
from brickwright.tools.placement_evidence import PlacementEvidence

from .artifacts import DecorativeInterface, Instance, Plan
from .gap_references import GapReference, RelativePose, gap_references

Leaf = Placement[Element]
_CANDIDATE_LIMIT = 33


class Location(Instance):
    color_id: int


class Component(BaseModel):
    members: list[int]
    part_count: int
    bounds: dict[str, list[float]]
    contacts: list[tuple[int, int]]


class Column(BaseModel):
    sdk_version: str
    catalog_version: str
    catalog_sha256: str
    part_count: int
    instances: list[Location]
    components: list[Component]
    collisions: list[tuple[int, int]]
    floating: list[int]
    contact_only_parts: list[int]
    interface_evidence: dict[str, dict[str, Any]]
    warnings: list[str] = Field(default_factory=list)
    modules: list[dict[str, Any]] = Field(default_factory=list)


class StructureReport(BaseModel):
    schema_version: Literal[1] = 1
    mpd_sha256: str | None
    native: Column | None
    standard: Column | None
    complete: bool
    passed: bool
    differences: dict[str, Any]
    classifications: list[dict[str, Any]]
    limitations: list[dict[str, Any]]
    diagnostics: list[Diagnostic]


def inspect_build(
    build: LegoBuild, plan: Plan | None = None, *, standard: bool = False
) -> Column:
    """Describe a built model. Instance IDs in the column index its instances array."""
    leaves = build.flattened_placements
    ids = {leaf: index for index, leaf in enumerate(leaves)}
    evidence = PlacementEvidence(build)
    instances = [
        Location.model_validate(
            evidence.placement(leaf)
            | {
                "rotation": leaf.pose.rotation.entries,
                "color_id": leaf.content.color.ldraw_id,
            }
        )
        for leaf in leaves
    ]
    contacts = [
        (ids[support], ids[resting])
        for support, resting in build.component_contact_pairs
    ]
    groups = build.connected_components
    components = []
    for group in groups:
        members = [ids[leaf] for leaf in group]
        indices = set(members)
        boxes = [leaf.bounding_box for leaf in group]
        components.append(
            Component(
                members=members,
                part_count=len(group),
                bounds={
                    "minimum": [
                        float(min(box[0][axis] for box in boxes)) for axis in range(3)
                    ],
                    "maximum": [
                        float(max(box[1][axis] for box in boxes)) for axis in range(3)
                    ],
                },
                contacts=[pair for pair in contacts if indices.intersection(pair)],
            )
        )
    direct_mates = {frozenset((ids[a], ids[b])) for a, b in build.mated_placement_pairs}
    contact_pairs = {frozenset(pair) for pair in contacts}
    interfaces = (
        {
            interface.id: _interface_evidence(
                interface, leaves, instances, direct_mates, contact_pairs, standard
            )
            for interface in plan.decorative_interfaces
        }
        if plan is not None
        else {}
    )
    return Column(
        sdk_version=sdk_version(),
        catalog_version=OFFICIAL_PARTS_VERSION,
        catalog_sha256=catalog_sha256(),
        part_count=len(leaves),
        instances=instances,
        components=components,
        collisions=[(ids[a], ids[b]) for a, b in build.colliding_placements],
        floating=[ids[leaf] for leaf in build.floating_placements],
        contact_only_parts=[ids[leaf] for leaf in build.contact_only_placements],
        interface_evidence=interfaces,
    )


def inspect_mpd(
    mpd: bytes,
    plan: Plan | None = None,
    merge: Any = None,
    *,
    ldraw_library: str = "",
) -> tuple[Column, list[Diagnostic]]:
    """Read original MPD bytes using only SDK data in this fresh worker."""
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", LDrawImportWarning)
        build = from_ldraw(mpd.decode("utf-8"), ldraw_library=ldraw_library)
    column = inspect_build(build, plan, standard=True)
    column.warnings = [
        str(warning.message)
        for warning in captured
        if issubclass(warning.category, LDrawImportWarning)
    ]
    from .integrations import merge_evidence

    column.modules, diagnostics = merge_evidence(build, merge)
    return column, diagnostics


def _same_vector(
    a: tuple[float, ...], b: tuple[float, ...], *, tolerance: float = 1e-5
) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b, strict=True))


def _same_pose(a: Instance, b: Instance) -> bool:
    return (
        a.part_id == b.part_id
        and _same_vector(a.position, b.position)
        and all(
            _same_vector(row_a, row_b)
            for row_a, row_b in zip(a.rotation, b.rotation, strict=True)
        )
    )


def _resolve(selector: Instance, instances: list[Location]) -> int | None:
    matches = [
        index
        for index, instance in enumerate(instances)
        if _same_pose(selector, instance)
        and (selector.path is None or selector.path == instance.path)
    ]
    return matches[0] if len(matches) == 1 else None


def _interface_evidence(
    interface: DecorativeInterface,
    leaves: tuple[Leaf, ...],
    instances: list[Location],
    direct_mates: set[frozenset[int]],
    contacts: set[frozenset[int]],
    standard: bool,
) -> dict[str, Any]:
    host_id = _resolve(interface.host, instances)
    child_id = _resolve(interface.child, instances)
    details: dict[str, Any] = {"host": host_id, "child": child_id, "mated": False}
    if host_id is None or child_id is None or host_id == child_id:
        return details
    host, child = leaves[host_id], leaves[child_id]
    pair = frozenset((host_id, child_id))
    parent_raw = partdata.record(host.content.ldraw_id).ldraw_pose(host.pose)
    child_raw = partdata.record(child.content.ldraw_id).ldraw_pose(child.pose)
    matrix, position = pose_to_ldraw(parent_raw.inverse.compose(child_raw))
    parent_endpoints = tuple(host.content.connectors)
    child_endpoints = tuple(child.content.connectors)
    compatible = [
        (a, b)
        for a in parent_endpoints
        for b in child_endpoints
        if mating_rule(a.type, b.type) is not None
    ]
    details.update(
        mated=pair in direct_mates,
        direct_contact=pair in contacts,
        relative_ldraw_pose={
            "position": position.tolist(),
            "rotation": matrix.tolist(),
        },
        host_endpoints=_ordered_endpoints(parent_endpoints),
        child_endpoints=_ordered_endpoints(child_endpoints),
        compatible_endpoint_pairs=len(compatible),
    )
    if standard and interface.gap and not details["mated"]:
        scratch = LegoBuild()
        placement = scratch.add_raw(host.content, host.pose)
        candidates = scratch.find_connection_candidates(
            placement,
            content=child.content,
            limit=_CANDIDATE_LIMIT,
        )
        trials = scratch.trial_connections(candidates, limit=_CANDIDATE_LIMIT)
        details.update(
            search_complete=len(candidates) < _CANDIDATE_LIMIT,
            candidate_count=len(candidates),
            trials=[
                {
                    "parent": connector_evidence(trial.candidate.parent.connector),
                    "child": connector_evidence(trial.candidate.child.connector),
                    "diagnostic_codes": [error.code for error in trial.diagnostics],
                }
                for trial in trials
            ],
        )
    return details


def _ordered_endpoints(endpoints) -> list[dict[str, Any]]:
    return sorted(
        (connector_evidence(connector) for connector in endpoints),
        key=repr,
    )


@cache
def sdk_version() -> str:
    return version("bricklayer-sdk")


@cache
def catalog_sha256() -> str:
    path = files("bricklayer.ldraw") / f"official_catalog_{OFFICIAL_PARTS_VERSION}.json"
    return sha256(path.read_bytes()).hexdigest()


def _matches_reference(pose: RelativePose, reference: GapReference) -> bool:
    tolerance = reference.tolerance
    return any(
        _same_vector(pose.position, candidate.position, tolerance=tolerance)
        and all(
            _same_vector(a, b, tolerance=tolerance)
            for a, b in zip(pose.rotation, candidate.rotation, strict=True)
        )
        for candidate in reference.poses
    )


def _proven_gap(
    declaration: DecorativeInterface,
    native: dict[str, Any],
    standard: dict[str, Any],
    catalog: Column,
) -> GapReference | None:
    if (
        declaration.required
        or not declaration.source
        or not standard.get("direct_contact")
    ):
        return None
    if not standard.get("search_complete") or not (
        native.get("direct_contact") or native.get("mated")
    ):
        return None
    pose = RelativePose.model_validate(standard["relative_ldraw_pose"])
    for reference in gap_references():
        if (
            (declaration.host.part_id, declaration.child.part_id)
            != (reference.host, reference.child)
            or (declaration.source, declaration.gap)
            != (reference.source, reference.gap)
            or (catalog.catalog_version, catalog.catalog_sha256)
            != (reference.catalog_version, reference.catalog_sha256)
            or standard["candidate_count"] != reference.candidate_count
            or not _matches_reference(pose, reference)
        ):
            continue
        if (
            reference.gap == "missing_endpoint"
            and standard["compatible_endpoint_pairs"] == 0
        ):
            return reference
        if (
            reference.gap == "endpoint_offset"
            and native["mated"]
            and native["host_endpoints"] != standard["host_endpoints"]
            and native["child_endpoints"] == standard["child_endpoints"]
            and standard["trials"]
            and all(
                trial["diagnostic_codes"] == ["assembly.collision"]
                for trial in standard["trials"]
            )
        ):
            return reference
    return None


def _intentionally_independent(
    plan: Plan, native: Column, standard: Column
) -> dict[str, dict[str, int]]:
    declared: dict[str, dict[str, int]] = {}
    for obj in plan.independent_objects:
        matching = {}
        for name, column in (("native", native), ("standard", standard)):
            resolved = {_resolve(member, column.instances) for member in obj.members}
            if None in resolved or len(resolved) != len(obj.members):
                continue
            for index, group in enumerate(column.components):
                if set(group.members) == resolved:
                    matching[name] = index
                    break
        if len(matching) == 2:
            declared[obj.id] = matching
    return declared


def classify_structure(
    native: Column | None,
    standard: Column | None,
    mpd: bytes | None,
    plan: Plan | None = None,
    diagnostics: list[Diagnostic] | None = None,
) -> StructureReport:
    """Apply U-09 to exact components in both observed columns."""
    gate_issues = diagnostics or []
    diagnostics = list(gate_issues)
    classifications = []
    limitations = []
    differences = {}
    complete = (
        native is not None
        and standard is not None
        and mpd is not None
        and not any(
            not message.startswith("used neutral appearance for")
            for message in standard.warnings
        )
        and native.part_count > 0
        and native.part_count == standard.part_count
        and all(
            _same_pose(first, second) and first.color_id == second.color_id
            for first, second in zip(native.instances, standard.instances, strict=True)
        )
    )
    physical_ok = False
    blocked = False
    if not complete:
        diagnostics.append(
            Diagnostic(
                code="structure.incomplete",
                stage="structure",
                message="The standard-catalog reread did not account for every original part and pose.",
                evidence={"import_warnings": standard.warnings[:8] if standard else []},
                next_action="Resolve import warnings and validate the current record again.",
            )
        )
    if native is not None and standard is not None:
        differences = {
            "part_count": [native.part_count, standard.part_count],
            "component_count": [len(native.components), len(standard.components)],
            "collision_count": [len(native.collisions), len(standard.collisions)],
            "floating_count": [len(native.floating), len(standard.floating)],
        }
        physical_ok = not any(
            (native.collisions, standard.collisions, native.floating, standard.floating)
        )
        if not physical_ok:
            diagnostics.append(
                Diagnostic(
                    code="structure.physical_checks",
                    stage="structure",
                    message="Both structure columns must be free of collisions and floating parts.",
                    evidence=differences,
                    next_action="Fix the collisions or floating parts listed in the structure report and validate again.",
                )
            )
        declared = _intentionally_independent(plan, native, standard) if plan else {}
        for obj in plan.independent_objects if plan else []:
            if obj.id not in declared:
                blocked = True
                diagnostics.append(
                    Diagnostic(
                        code="structure.unattached_component",
                        stage="structure",
                        message=f"Independent object {obj.id!r} does not exactly match a component in both columns.",
                        evidence={
                            "object_id": obj.id,
                            "declared_parts": len(obj.members),
                        },
                        next_action="Check placement and rotation for every declared member in the two structure columns.",
                    )
                )
        good_gaps: dict[str, dict[str, Any]] = {}
        for interface in plan.decorative_interfaces if plan else []:
            before = native.interface_evidence[interface.id]
            after = standard.interface_evidence[interface.id]
            issue = {
                "interface_id": interface.id,
                "native": before,
                "standard": after,
                "reason": interface.reason,
                "source": interface.source,
                "gap": interface.gap,
                "catalog_version": standard.catalog_version,
            }
            if before["mated"] and after["mated"]:
                continue
            proof = _proven_gap(interface, before, after, standard)
            if complete and physical_ok and proof is not None:
                good_gaps[interface.id] = issue | {
                    "reference": proof.model_dump(mode="json")
                }
            else:
                blocked = True
                diagnostics.append(
                    Diagnostic(
                        code="structure.unattached_component",
                        stage="structure",
                        message=f"Interface {interface.id!r} has no proven connection in both columns.",
                        evidence={
                            "interface_id": interface.id,
                            "required": interface.required,
                        },
                        next_action="Check the declared instance poses with find_connection_candidates and trial_connections.",
                    )
                )
        used_gaps = set()
        unknown = []
        contact_only = []
        for name, column in (("native", native), ("standard", standard)):
            if not column.components:
                continue
            attached = {by_column[name] for by_column in declared.values()}
            primary = max(
                (
                    index
                    for index in range(len(column.components))
                    if index not in attached
                ),
                key=lambda index: column.components[index].part_count,
                default=None,
            )
            if primary is not None:
                attached.add(primary)
            classifications.extend(
                {
                    "column": name,
                    "component": by_column[name],
                    "kind": "intentional",
                    "object_id": obj_id,
                }
                for obj_id, by_column in declared.items()
            )
            component_of = {
                member: index
                for index, group in enumerate(column.components)
                for member in group.members
            }
            for _ in column.components:
                prior = len(attached)
                for interface_id, gap in good_gaps.items():
                    interface = gap[name]
                    host = component_of.get(interface["host"])
                    child = component_of.get(interface["child"])
                    if host in attached and child not in attached and child is not None:
                        attached.add(child)
                        used_gaps.add(interface_id)
                        classifications.append(
                            {
                                "column": name,
                                "component": child,
                                "kind": "data_gap",
                                "interface_id": interface_id,
                            }
                        )
                if len(attached) == prior:
                    break
            for index, group in enumerate(column.components):
                if index not in attached:
                    classifications.append(
                        {"column": name, "component": index, "kind": "unknown"}
                    )
                    unknown.append(
                        {
                            "column": name,
                            "component": index,
                            "part_count": group.part_count,
                            "instances": group.members[:4],
                            "contacts": [list(pair) for pair in group.contacts[:4]],
                        }
                    )
            for part in column.contact_only_parts:
                contact_only.append({"column": name, "instance": part})
        if unknown:
            blocked = True
            diagnostics.append(
                Diagnostic(
                    code="structure.unattached_component",
                    stage="structure",
                    message=f"{len(unknown)} component observations have no matching independent object or proven interface.",
                    evidence={"total": len(unknown), "samples": unknown[:8]},
                    next_action="Locate each component in the structure report and check its intended attachment with find_connection_candidates.",
                )
            )
        limitations = [
            gap
            for interface_id, gap in good_gaps.items()
            if interface_id in used_gaps
            or not all(gap[name]["mated"] for name in ("native", "standard"))
        ]
        if limitations:
            diagnostics.append(
                Diagnostic(
                    code="structure.data_gap_component",
                    stage="structure",
                    message="Declared optional decoration interfaces have reproducible standard-catalog gaps.",
                    evidence={
                        "interface_ids": [item["interface_id"] for item in limitations]
                    },
                    next_action="Include these specific interface limitations in the delivery report.",
                )
            )
        if contact_only:
            diagnostics.append(
                Diagnostic(
                    code="structure.contact_only_part",
                    stage="structure",
                    message=f"{len(contact_only)} part observations are supported by contact without a connection.",
                    evidence={"total": len(contact_only), "samples": contact_only[:8]},
                )
            )
    passed = complete and physical_ok and not blocked and not gate_issues
    return StructureReport(
        mpd_sha256=sha256(mpd).hexdigest() if mpd is not None else None,
        native=native,
        standard=standard,
        complete=complete,
        passed=passed,
        differences=differences,
        classifications=classifications,
        limitations=limitations,
        diagnostics=diagnostics,
    )
