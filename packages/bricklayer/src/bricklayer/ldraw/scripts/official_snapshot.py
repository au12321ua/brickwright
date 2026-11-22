"""Read and verify the official visual part definitions for a release index."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from hashlib import sha256
from pathlib import Path

from ...parts.official import CapabilityStatus, PartKind, _PartRow
from ..naming import part_name
from .fallbacks import pattern_base
from .library import redirect_target

_RELEASE_SOURCE = "https://library.ldraw.org/updates"
_RELEASE_ANNOUNCEMENT = "https://forums.ldraw.org/thread-29680.html"
# Content hashes of the LDraw 2026-10 delivery, independent of ZIP metadata.
# The include set is every file under parts/ and p/ (including dependencies).
_FROZEN_FILES_SHA256 = (
    "332b133ffc9afb7584529b8425e9b7972f6599f8db469910845a83247b83cf74"
)
_FROZEN_IDS_SHA256 = "63cdbdf87016028366be21a7444e0fccac4fd135cda71b2de39e2135d7dadf82"

_GEOMETRY = re.compile(rb"^[ \t]*(?:0[ \t]+!:[ \t]+)?([1-5])[ \t]+(.+)", re.MULTILINE)
_TEXMAP = re.compile(rb"^0[ \t]+!TEXMAP[ \t]+(?:START|NEXT)[ \t]+(.+)", re.MULTILINE)
_HEADER_FIELDS = {
    "0 Author:": "author",
    "0 !CATEGORY ": "category",
    "0 !LDRAW_ORG ": "type",
    "0 !LICENSE ": "license",
}


@dataclass(frozen=True)
class Snapshot:
    """An official source tree fingerprint and every top-level part."""

    file_count: int
    sha256: str
    ids_sha256: str
    parts: dict[str, _PartRow]

    def verify(self) -> None:
        """Reject a modified or different release before generating its index."""
        if (self.sha256, self.ids_sha256) != (_FROZEN_FILES_SHA256, _FROZEN_IDS_SHA256):
            raise ValueError(
                "The LDraw source does not match the frozen 2026-10 snapshot. "
                f"Expected files SHA-256 {_FROZEN_FILES_SHA256}, got {self.sha256}. "
                f"Expected top-level IDs SHA-256 {_FROZEN_IDS_SHA256}, "
                f"got {self.ids_sha256}."
            )

    def provenance(self) -> dict[str, str | int]:
        """Source and digest information saved with the generated catalog."""
        return {
            "source": _RELEASE_SOURCE,
            "release_announcement": _RELEASE_ANNOUNCEMENT,
            "scope": "All files in parts/ and p/. IDs are the stems of top-level parts/*.dat.",
            "tree_sha256": self.sha256,
            "id_sha256": self.ids_sha256,
            "file_count": self.file_count,
            "id_count": len(self.parts),
        }


@dataclass
class _Definition:
    references: tuple[str, ...]
    surfaces: bool
    wrapper: tuple[str, list[float]] | None = None


def scan_snapshot(root: Path) -> Snapshot:
    """Fingerprint and record top-level parts, resolving visual dependencies.

    The tree digest hashes UTF-8 relative paths, NUL separators, and the binary
    content SHA-256 for each sorted file. The membership digest hashes sorted
    top-level IDs with one UTF-8 ID per line.
    """
    paths = sorted(
        path.relative_to(root).as_posix()
        for folder in ("parts", "p")
        for path in (root / folder).rglob("*")
        if path.is_file()
    )
    tree_hash = sha256()
    entries: dict[str, _PartRow] = {}
    definitions: dict[str, _Definition] = {}
    for path in paths:
        body = (root / path).read_bytes()
        digest = sha256(body)
        tree_hash.update(path.encode("utf-8") + b"\0" + digest.digest())
        if not path.casefold().endswith(".dat"):
            continue
        part_id = _top_id(path)
        definition = _definition(body, part_id is not None)
        definitions[path.casefold()] = definition
        if part_id is not None:
            entries[part_id] = _header(body, digest.hexdigest())

    files = frozenset(path.casefold() for path in paths)

    @cache
    def resolve(name: str) -> str | None:
        directories = (
            ("parts/textures", "parts")
            if name.endswith(".png")
            else (
                "parts",
                "parts/s",
                "p",
            )
        )
        for directory in directories:
            if (path := f"{directory}/{name}") in files:
                return path
        return None

    checked: dict[str, tuple[bool, frozenset[str]]] = {}

    def describe(path: str, visiting: set[str]) -> tuple[bool, frozenset[str]]:
        if path in checked:
            return checked[path]
        if path in visiting:
            return False, frozenset({f"{path} (cyclic reference)"})
        visiting.add(path)
        geometry = definitions[path]
        has_geometry = geometry.surfaces
        issues: set[str] = set()
        for reference in geometry.references:
            target = resolve(reference)
            if target is None:
                issues.add(reference)
            elif target in definitions:
                has_child_geometry, child_issues = describe(target, visiting)
                has_geometry |= has_child_geometry
                issues.update(child_issues)
        visiting.remove(path)
        checked[path] = has_geometry, frozenset(issues)
        return checked[path]

    for part_id, row in entries.items():
        path = f"parts/{part_id}.dat".casefold()
        definition = definitions[path]
        has_geometry, issues = describe(path, set())
        row["visual"] = _geometry_status(has_geometry, issues)
        if issues:
            row["geometry_issues"] = sorted(issues)
        if row["kind"] in (PartKind.ALIAS, PartKind.REDIRECT):
            if definition.wrapper is not None:
                reference, transform = definition.wrapper
                row["reference_id"] = reference.removesuffix(".dat")
                row["reference_ldraw"] = transform
            elif (target := redirect_target(row["name"])) is not None:
                row["reference_id"] = target.casefold()
        if row["kind"] == PartKind.ASSEMBLY:
            row["components"] = sorted(
                {
                    component
                    for reference in definition.references
                    if (target := resolve(reference)) is not None
                    and (component := _top_id(target)) is not None
                }
            )
        if (
            row["kind"] == PartKind.PATTERN
            and (base := pattern_base(part_id)) in entries
        ):
            row["base"] = base

    for part_id, row in entries.items():
        if row["kind"] in (PartKind.ALIAS, PartKind.REDIRECT):
            row["canonical_id"] = _canonical_id(part_id, entries)

    ids = "".join(f"{part_id}\n" for part_id in sorted(entries)).encode("utf-8")
    return Snapshot(len(paths), tree_hash.hexdigest(), sha256(ids).hexdigest(), entries)


def _top_id(path: str) -> str | None:
    if path.startswith("parts/") and path.count("/") == 1 and path.endswith(".dat"):
        return path.removeprefix("parts/").removesuffix(".dat")
    return None


def _normalized_ref(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip().replace("\\", "/").casefold()


def _definition(body: bytes, top_level: bool) -> _Definition:
    references: set[str] = set()
    surfaces = False
    wrapper_fields: list[bytes] | None = None
    instances = 0
    for match in _GEOMETRY.finditer(body):
        if match[1] != b"1":
            surfaces = True
            continue
        fields = match[2].split(maxsplit=13)
        if len(fields) != 14:
            continue
        references.add(_normalized_ref(fields[13]))
        instances += 1
        if top_level:
            wrapper_fields = fields
    wrapper = None
    if (
        wrapper_fields is not None
        and wrapper_fields[0] == b"16"
        and instances == 1
        and not surfaces
    ):
        target = _normalized_ref(wrapper_fields[13])
        try:
            wrapper = (target, list(map(float, wrapper_fields[1:13])))
        except ValueError:
            pass  # retain the unresolved visual definition's explicit references
    for texture in _TEXMAP.finditer(body):
        references.update(
            _normalized_ref(name)
            for name in texture[1].split()
            if name.lower().endswith(b".png")
        )
    return _Definition(tuple(sorted(references)), surfaces, wrapper)


def _header(body: bytes, digest: str) -> _PartRow:
    lines = body.split(b"\n", maxsplit=20)
    description = (
        lines[0].decode("utf-8-sig", errors="replace").strip().removeprefix("0 ")
    )
    header: dict[str, str] = {}
    for raw in lines[1:]:
        if not raw.strip():
            continue
        if not raw.startswith(b"0"):
            break
        text = raw.decode("utf-8", errors="replace").strip()
        for prefix, key in _HEADER_FIELDS.items():
            if text.startswith(prefix):
                header[key] = text.removeprefix(prefix).strip()

    name = part_name(description)
    file_type = header.get("type", "")
    kind = _kind(description, name, file_type)
    first_word = name.split(maxsplit=1)[0] if name else "Uncategorized"
    return {
        "name": name,
        "kind": kind,
        "category": header.get("category", first_word),
        "ldraw_type": file_type,
        "author": header.get("author", ""),
        "license": header.get("license", ""),
        "sha256": digest,
        "visual": CapabilityStatus.UNVERIFIED,
        "model": False,
    }


def _kind(description: str, name: str, file_type: str) -> PartKind:
    flags = file_type.split(" UPDATE ")[0].split()
    if description.startswith("~Moved to "):
        return PartKind.REDIRECT
    if description.startswith("=") or "Alias" in flags:
        return PartKind.ALIAS
    if name.startswith("Sticker "):
        return PartKind.STICKER
    if "Shortcut" in flags or "(Shortcut)" in name:
        return PartKind.ASSEMBLY
    if "Flexible_Section" in flags:
        return PartKind.FLEXIBLE_SECTION
    if "Physical_Colour" in flags:
        return PartKind.PHYSICAL_COLOUR
    if name.endswith(" Pattern") or " Sticker" in name:
        return PartKind.PATTERN
    return PartKind.PART


def _canonical_id(part_id: str, entries: dict[str, _PartRow]) -> str | None:
    visited: set[str] = set()
    while part_id in entries and part_id not in visited:
        row = entries[part_id]
        if row["kind"] not in (PartKind.ALIAS, PartKind.REDIRECT):
            return part_id
        visited.add(part_id)
        part_id = row.get("reference_id", "")
    return None


def _geometry_status(has_geometry: bool, issues: frozenset[str]) -> CapabilityStatus:
    if has_geometry:
        return CapabilityStatus.PARTIAL if issues else CapabilityStatus.AVAILABLE
    return CapabilityStatus.MISSING if issues else CapabilityStatus.UNVERIFIED
