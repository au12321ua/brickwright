"""Observe physical occurrences and coverage in the frozen LDraw shortcut set."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256
from math import isfinite
from pathlib import Path

from ...geometry import Mat3, Pose, Vec3
from ...io import from_ldraw
from ...modeling.placement import Placement
from ...parts import Element
from ...parts.catalog import part_exists
from ...parts.official import OFFICIAL_PARTS_VERSION, PartKind
from ..colors import parse_color_code
from ..conversion import INHERIT_COLOR, pose_from_type1
from ..library import ImportLibrary
from .official_snapshot import Snapshot

_IDENTITY = (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
INVENTORY_RULE_VERSION = "p007-2026-10-1"


@dataclass(frozen=True)
class SourceLine:
    source: str
    line_number: int
    raw: str
    sha256: str
    author: str
    license: str


@dataclass(frozen=True)
class Instance:
    part_id: str
    color: int
    transform: tuple[float, ...]
    path: tuple[SourceLine, ...]

    @property
    def pose(self) -> Pose:
        """Composed pose of the original part frame."""
        return pose_from_type1(self.transform)

    def placement(self, root: Path) -> Placement[Element]:
        """Placement using the SDK's seated local part frame."""
        numbers = " ".join(map(str, self.transform))
        placements = from_ldraw(
            f"1 {self.color} {numbers} {self.part_id}.dat", ldraw_library=root
        ).placements
        if len(placements) == 1:
            placement = placements[0]
            if isinstance(content := placement.content, Element):
                return Placement(content, placement.pose)
        raise ValueError(f"Cannot import {self.part_id} as one element")

    def relative_to(self, host: Instance) -> Pose:
        return host.pose.inverse.compose(self.pose)


@dataclass(frozen=True)
class Issue:
    code: str
    detail: str
    path: tuple[SourceLine, ...]


@dataclass(frozen=True)
class Assembly:
    root_lines: tuple[SourceLine, ...]
    direct: tuple[Instance, ...]
    instances: tuple[Instance, ...]
    issues: tuple[Issue, ...]


@dataclass(frozen=True)
class CoveredInterface:
    sample_id: str
    family: str
    host: Instance
    child: Instance


def assembly_ids(snapshot: Snapshot) -> tuple[str, ...]:
    """Unique IDs included by the P-007 Shortcut/assembly rule."""
    return tuple(
        part_id
        for part_id, row in sorted(snapshot.parts.items())
        if row["kind"] == PartKind.ASSEMBLY
        or row["ldraw_type"].split()[:1] == ["Shortcut"]
    )


def _type1_lines(
    root: Path, part_id: str, snapshot: Snapshot
) -> tuple[SourceLine, ...]:
    source = f"parts/{part_id}.dat"
    row = snapshot.parts[part_id]
    body = (root / source).read_bytes()
    digest = sha256(body).hexdigest()
    if digest != row["sha256"]:
        raise ValueError(f"LDraw file content changed after fingerprinting: {source}")
    return tuple(
        SourceLine(source, number, line, digest, row["author"], row["license"])
        for number, line in enumerate(
            body.decode("utf-8-sig", errors="replace").splitlines(), 1
        )
        if _type1_text(line).split(maxsplit=1)[:1] == ["1"]
    )


def _type1_text(line: str) -> str:
    fields = line.split(maxsplit=2)
    if len(fields) == 3 and fields[:2] == ["0", "!:"]:
        return fields[2]
    return line.lstrip()


def _parse(line: SourceLine) -> tuple[int, tuple[float, ...], str]:
    fields = _type1_text(line.raw).split(maxsplit=14)
    if len(fields) != 15:
        raise ValueError("A type-1 line requires a color, 12 numbers and a filename")
    color = parse_color_code(fields[1])
    transform = tuple(map(float, fields[2:14]))
    if color < 0 or not all(map(isfinite, transform)):
        raise ValueError("A type-1 line requires a valid color and finite transform")
    target = fields[14].strip().replace("\\", "/").casefold()
    return color, transform, target


def _compose(parent: tuple[float, ...], child: tuple[float, ...]) -> tuple[float, ...]:
    if parent == _IDENTITY:
        return child
    rotation = Mat3.from_flattened(parent[3:])
    moved = Vec3(*parent[:3]) + rotation @ Vec3(*child[:3])
    matrix = rotation @ Mat3.from_flattened(child[3:])
    return (*moved, *matrix.flatten())


def read_instances(
    root: Path, part_id: str, snapshot: Snapshot
) -> tuple[Instance, ...]:
    """Return physical leaf occurrences in source order."""
    return read_assembly(root, part_id, snapshot).instances


def read_assembly(root: Path, part_id: str, snapshot: Snapshot) -> Assembly:
    """Keep direct references and unfold nested assemblies along occurrence paths.

    Geometric subparts and primitives are retained as observations. Nonrigid
    matrices retain their raw values, including in paths where the SDK cannot
    express the resulting transform.
    """
    direct: list[Instance] = []
    instances: list[Instance] = []
    issues: list[Issue] = []

    def visit(
        lines: tuple[SourceLine, ...],
        parent_path: tuple[SourceLine, ...],
        parent_color: int,
        parent_transform: tuple[float, ...],
        ancestors: frozenset[str],
    ) -> None:
        for line in lines:
            path = (*parent_path, line)
            try:
                color, transform, target = _parse(line)
            except ValueError as error:
                issues.append(Issue("malformed_type1", str(error), path))
                continue
            child_id = target.removesuffix(".dat")
            child_row = snapshot.parts.get(child_id)
            if child_row is None or not target.endswith(".dat"):
                if any(
                    (root / name / target).is_file()
                    for name in ("parts", "parts/s", "p")
                ):
                    code = "geometry_only"
                else:
                    code = "unresolved_reference"
                issues.append(Issue(code, target, path))
                continue
            color = parent_color if color == INHERIT_COLOR else color
            transform = _compose(parent_transform, transform)
            item = Instance(child_id, color, transform, path)
            try:
                pose_from_type1(item.transform)
            except ValueError:
                issues.append(Issue("non_rigid_transform", child_id, path))
            if not parent_path:
                direct.append(item)
            if child_row["kind"] == PartKind.ASSEMBLY:
                if child_id in ancestors:
                    issues.append(Issue("cyclic_reference", child_id, path))
                else:
                    visit(
                        _type1_lines(root, child_id, snapshot),
                        path,
                        color,
                        transform,
                        ancestors | {child_id},
                    )
            else:
                instances.append(item)

    root_lines = _type1_lines(root, part_id, snapshot)
    visit(root_lines, (), INHERIT_COLOR, _IDENTITY, frozenset({part_id}))
    return Assembly(root_lines, tuple(direct), tuple(instances), tuple(issues))


def scan_inventory(
    root: Path,
    snapshot: Snapshot,
    coverage: Mapping[str, Sequence[CoveredInterface]],
) -> list[dict[str, object]]:
    """Give every frozen shortcut one row and keep all identifiable interfaces."""
    library = ImportLibrary(root)
    return [
        _inventory_row(
            part_id,
            snapshot,
            library,
            read_assembly(root, part_id, snapshot),
            coverage.get(part_id, ()),
        )
        for part_id in assembly_ids(snapshot)
    ]


def _inventory_row(
    part_id: str,
    snapshot: Snapshot,
    library: ImportLibrary,
    observed: Assembly,
    coverage: Sequence[CoveredInterface],
) -> dict[str, object]:
    source = snapshot.parts[part_id]
    known_paths = {item.path for row in coverage for item in (row.host, row.child)}
    interfaces = [
        {
            "status": "direct",
            "family": row.family,
            "sample_id": row.sample_id,
            "host": _occurrence(row.host),
            "child": _occurrence(row.child),
        }
        for row in coverage
    ]
    uncovered = [item for item in observed.instances if item.path not in known_paths]
    interfaces.extend(
        {
            **_screen_occurrence(item, snapshot, library),
            "occurrence": _occurrence(item),
        }
        for item in uncovered
    )
    if not interfaces:
        interfaces.append(
            {
                "status": "insufficient_evidence",
                "family": "unidentified",
                "reason": "no_physical_interface_observed",
            }
        )
    issue_codes = {issue.code for issue in observed.issues}
    is_partial = any(
        row["reason"] != "decorative_only" for row in interfaces[len(coverage) :]
    ) or bool(issue_codes - {"geometry_only"})
    status = "unverified" if not coverage else "partial" if is_partial else "covered"
    return {
        "assembly_id": part_id,
        "name": source["name"],
        "kind": source["kind"],
        "ldraw_type": source["ldraw_type"],
        "category": source["category"],
        "release": OFFICIAL_PARTS_VERSION,
        "rule_version": INVENTORY_RULE_VERSION,
        "source": f"parts/{part_id}.dat",
        "sha256": source["sha256"],
        "author": source["author"],
        "license": source["license"],
        "reference_id": source.get("reference_id"),
        "canonical_id": source.get("canonical_id", part_id),
        "reference_transform_ldraw": source.get("reference_ldraw"),
        "root_lines": [asdict(line) for line in observed.root_lines],
        "issues": [asdict(issue) for issue in observed.issues],
        "geometry_issues": source.get("geometry_issues", []),
        "instances": [asdict(instance) for instance in observed.instances],
        "interfaces": interfaces,
        "coverage": status,
    }


def _occurrence(item: Instance) -> dict[str, object]:
    return {
        "part_id": item.part_id,
        "path": [(line.source, line.line_number) for line in item.path],
    }


def _screen_occurrence(
    item: Instance, snapshot: Snapshot, library: ImportLibrary
) -> dict[str, str]:
    source = snapshot.parts[item.part_id]
    kind = source["kind"]
    name = source["name"].casefold()
    if kind == PartKind.STICKER:
        return {
            "status": "insufficient_evidence",
            "family": "decoration",
            "reason": "decorative_only",
        }
    if kind == PartKind.FLEXIBLE_SECTION or name.startswith(
        ("hose ", "string ", "cloth ")
    ):
        return {
            "status": "needs_rule",
            "family": "flexible",
            "reason": "flexible_shape_required",
        }
    if (
        kind in (PartKind.REDIRECT, PartKind.ALIAS)
        and source.get("canonical_id") is None
    ):
        return {
            "status": "insufficient_evidence",
            "family": "unidentified",
            "reason": "unresolved_identity",
        }
    if not _can_import(item.part_id, library):
        return {
            "status": "insufficient_evidence",
            "family": "unidentified",
            "reason": "sdk_import_unavailable",
        }
    for words, family in (
        (("wheel", "tyre"), "wheel"),
        (("hinge", "pivot"), "hinge"),
        (("axle", "pin", "bush"), "shaft_or_pin"),
        (("ball", "socket"), "ball_socket"),
        (("door", "window", "windscreen"), "panel_mount"),
    ):
        if any(word in name.split() for word in words):
            return {
                "status": "needs_rule",
                "family": family,
                "reason": "interface_rule_required",
            }
    return {
        "status": "insufficient_evidence",
        "family": "unidentified",
        "reason": "cooccurrence_only",
    }


def _can_import(part_id: str, library: ImportLibrary) -> bool:
    """Use the reader's same rigid-alias rule when the raw ID is absent."""
    seen: set[str] = set()
    while not part_exists(part_id):
        if part_id in seen or (target := library.redirect(part_id)) is None:
            return False
        seen.add(part_id)
        part_id, _ = target
    return True
