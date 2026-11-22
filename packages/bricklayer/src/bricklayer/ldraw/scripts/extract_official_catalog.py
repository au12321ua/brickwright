"""Regenerate, check, or compare the versioned official LDraw part index."""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from stat import S_IMODE
from zipfile import ZipFile

import numpy as np

from ...geometry import Connector, Pose
from ...parts.mountings import verified_mountings
from ...parts.official import (
    _CATALOG_SCHEMA,
    OFFICIAL_PARTS_VERSION,
    PartCapabilities,
    _CatalogData,
    _ConnectorSourceRow,
    _PartRow,
)
from .. import partdata
from ..connectivity import ConnectorRaw, decode_connectors
from ..conversion import pose_to_ldraw
from ..meshes import DATA_DIR, default_store
from .library import Library
from .official_snapshot import scan_snapshot

_OUTPUT = DATA_DIR / f"official_catalog_{OFFICIAL_PARTS_VERSION}.json"
_SUPPLEMENT = "official_part_supplements.json"
_MOUNTING_SOURCE = "../parts/mountings.py"
_IDENTITY_LDRAW = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


def build_catalog(ldraw_root: Path) -> _CatalogData:
    """Generate the index from the verified visual tree and bundled SDK inputs."""
    snapshot = scan_snapshot(ldraw_root)
    snapshot.verify()
    parts = snapshot.parts
    model_ids = partdata.part_ids()
    _add_collision_metadata(parts)
    supplements = _supplements(ldraw_root)
    for part_id, row in parts.items():
        row["model"] = part_id in model_ids
        _add_sdk_metadata(part_id, row)
        if (popular := partdata.popularity(part_id)) > 0:
            row["popularity"] = popular
        if supplement := supplements["parts"].get(part_id):
            if (collider := supplement.get("collider")) and (
                row.get("collider", [None, None])[1] != collider["frame_sha256"]
            ):
                raise ValueError(
                    f"Supplemental collision frame checksum mismatch for {part_id}"
                )
            row["supplemental"] = [_SUPPLEMENT, _digest(supplement)]

    return {
        "schema": _CATALOG_SCHEMA,
        "version": OFFICIAL_PARTS_VERSION,
        "inputs": {
            "ldraw": snapshot.provenance(),
            "bricknet": {
                "source": "https://github.com/kulits/BrickNet",
                "scope": "Packaged extraction of BrickNet labels and Draco collision frames.",
            },
            "packaged_files_sha256": _package_fingerprints(),
        },
        "parts": parts,
        "status_counts": {
            name: {status: len(ids) for status, ids in grouping.items()}
            for name, grouping in _capabilities(parts).items()
        },
        "mountings": _mountings(parts),
    }


def _supplements(ldraw_root: Path) -> dict:
    supplements = json.loads((DATA_DIR / _SUPPLEMENT).read_text(encoding="utf-8"))
    if supplements["ldraw_version"] != OFFICIAL_PARTS_VERSION:
        raise ValueError(f"{_SUPPLEMENT} is for a different LDraw release")
    for file, source in supplements["files"].items():
        if sha256((ldraw_root / file).read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError(f"Supplemental source checksum mismatch for {file}")
    return supplements


def _add_collision_metadata(parts: dict[str, _PartRow]) -> None:
    """Record the specific frame in the mesh store and its content hash."""
    store = default_store()
    by_category: dict[str, list[str]] = defaultdict(list)
    for part_id in sorted(parts.keys() & set(store.part_ids())):
        by_category[store.category(part_id)].append(part_id)
    for category, members in by_category.items():
        path = f"meshes/{category}.zip"
        with ZipFile(DATA_DIR / path) as archive:
            for part_id in members:
                member = f"{part_id}.drc"
                frame = archive.read(member)
                if not frame.startswith(b"DRACO"):
                    raise ValueError(f"Invalid Draco frame {path}/{member}")
                parts[part_id]["collider"] = [
                    f"{path}/{member}",
                    sha256(frame).hexdigest(),
                ]


def _add_sdk_metadata(part_id: str, row: _PartRow) -> None:
    generated: list[tuple[str, dict, dict[str, int]]] = []
    overrides: list[tuple[str, dict, dict[str, int]]] = []
    for file, is_override, entry in partdata._source_rows(
        part_id, partdata._DATA_FILES
    ):
        for key, value in entry.items():
            if key == "bbox":
                if value is not None:
                    row["bounds"] = [file, _digest(value)]
                else:
                    row.pop("bounds", None)
            elif key == "offset_rotation":
                row["frame"] = [file, _digest(value)]
            elif key in ("connectors", "extra_connectors"):
                if isinstance(value, dict):
                    counts = {name: len(rows) for name, rows in value.items() if rows}
                else:
                    counts = dict(Counter(raw["type"] for raw in value))
                contribution = (file, entry, counts)
                if key == "extra_connectors":
                    overrides = [contribution]
                elif is_override:
                    overrides.append(contribution)
                else:
                    generated = [contribution]
    sources: list[_ConnectorSourceRow] = []
    names: set[str] = set()
    for file, entry, counts in (*generated, *overrides):
        if not counts:
            continue
        names.update(counts)
        source: _ConnectorSourceRow = {
            "file": file,
            "count": sum(counts.values()),
            "sha256": _digest([entry, list(counts)]),
        }
        if donor := entry.get("_source"):
            source["donor_id"] = donor
            if transform := _recover_donor_transform(entry["connectors"], donor):
                source["transform_ldraw"] = transform
            else:
                source["note"] = (
                    "Donor named by the BrickNet extraction. Transform unrecorded."
                )
        if note := entry.get("_note"):
            source["note"] = note
        elif file.endswith("_overrides.json"):
            source["note"] = "Authored addition to the effective connector annotations."
        sources.append(source)
    if sources:
        unknown = names - partdata.all_connector_types().keys()
        if unknown:
            raise ValueError(
                f"Unregistered connector types for {part_id!r}: {sorted(unknown)}"
            )
        row["connector_types"] = sorted(names)
        row["connector_sources"] = sources


def _recover_donor_transform(raw: list | dict, donor_id: str) -> list[float] | None:
    """Recover an existing extract's donor transform from its explicit frames.

    The original extractor retains grouped rows only for identity transfers.
    For rotated or shifted transfers compare corresponding connectors against
    the original donor rows and record the transform when they agree.
    """
    if isinstance(raw, dict):
        return _IDENTITY_LDRAW
    donor = partdata._parts("part_connectivity.json").get(donor_id, {})
    original = donor.get("connectors")
    if not original or donor.get("_source"):
        return None
    source, target = _ordered_connectors(original), _ordered_connectors(raw)
    if [conn.type for conn in source] != [conn.type for conn in target]:
        return None
    first, moved = source[0], target[0]
    rotation = moved.frame @ first.frame.transpose
    translation = moved.position - Pose(rotation).apply(first.position)
    pose = Pose(rotation, translation)
    for before, after in zip(source, target, strict=True):
        transformed = before.transformed(pose)
        for actual, target_value in (
            (tuple(transformed.position), tuple(after.position)),
            (tuple(transformed.axis), tuple(after.axis)),
            (tuple(transformed.side), tuple(after.side)),
            (float(transformed.length), float(after.length)),
        ):
            if not np.allclose(actual, target_value, atol=1e-6, rtol=0):
                return None
    ld_rotation, ld_translation = pose_to_ldraw(pose)
    return [
        round(float(value), 10) + 0.0 for value in (*ld_translation, *ld_rotation.flat)
    ]


def _ordered_connectors(raw: list | dict) -> list[Connector]:
    normalized = partdata._normalized({"connectors": raw})["connectors"]
    connectors = decode_connectors(
        (ConnectorRaw.model_validate(row) for row in normalized),
        partdata.all_connector_types(),
    )
    return sorted(connectors, key=lambda connector: connector.type.name)


def _package_fingerprints() -> dict[str, str]:
    files = {
        "part_taxonomy.json",
        "part_connector_types.json",
        "part_popularity.json",
        "meshes/index.json",
        _SUPPLEMENT,
        _MOUNTING_SOURCE,
        *(file for file, _ in partdata._DATA_FILES),
        *(
            path.relative_to(DATA_DIR).as_posix()
            for path in (DATA_DIR / "meshes").glob("*.zip")
        ),
    }
    return {
        file: sha256((DATA_DIR / file).read_bytes()).hexdigest()
        for file in sorted(files)
    }


def _mountings(parts: dict[str, _PartRow]) -> dict[str, object]:
    result: dict[str, object] = {}
    for row in verified_mountings():
        key = "/".join((*row.part_ids, row.mounting))
        if key in result or any(
            part_id not in parts or not parts[part_id]["model"]
            for part_id in row.part_ids
        ):
            raise ValueError(f"Duplicate or unavailable verified mounting {key}")
        result[key] = {
            "endpoint_indices": list(row.endpoint_indices),
            "parameters": asdict(row.parameters),
            "rotation": [list(vector) for vector in row.pose.rotation.entries],
            "translation": list(map(float, row.pose.translation)),
            "verification_scope": row.verification_scope,
            "sources": list(row.sources),
            "motion": asdict(row.motion) if row.motion is not None else None,
            "samples_per_axis": row.samples_per_axis,
        }
    return result


def _capabilities(parts: dict[str, _PartRow]) -> dict[str, dict[str, list[str]]]:
    groups: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for part_id, row in sorted(parts.items()):
        part = PartCapabilities(part_id, OFFICIAL_PARTS_VERSION, row)
        for name, capability in (
            ("constructible", part.constructible),
            ("geometry", part.geometry),
            ("collision", part.collision),
            ("connectivity", part.connectivity),
        ):
            groups[name][capability.status].append(part_id)
    return {name: dict(sorted(statuses.items())) for name, statuses in groups.items()}


def diff_catalogs(before: _CatalogData, after: _CatalogData) -> dict:
    """Compare two generated artifacts without the original libraries."""
    old, new = before["parts"], after["parts"]
    common = old.keys() & new.keys()
    old_mountings, new_mountings = before["mountings"], after["mountings"]
    return {
        "version_before": before["version"],
        "version_after": after["version"],
        "changed_inputs": sorted(
            name
            for name in before["inputs"].keys() | after["inputs"].keys()
            if before["inputs"].get(name) != after["inputs"].get(name)
        ),
        "added_ids": sorted(new.keys() - old.keys()),
        "removed_ids": sorted(old.keys() - new.keys()),
        "changed_parts": {
            part_id: sorted(
                key
                for key in old[part_id].keys() | new[part_id].keys()
                if dict(old[part_id]).get(key) != dict(new[part_id]).get(key)
            )
            for part_id in sorted(common)
            if old[part_id] != new[part_id]
        },
        "newly_constructible": sorted(
            part_id
            for part_id, row in new.items()
            if row["model"] and (part_id not in old or not old[part_id]["model"])
        ),
        "no_longer_constructible": sorted(
            part_id
            for part_id, row in old.items()
            if row["model"] and (part_id not in new or not new[part_id]["model"])
        ),
        "newly_verified_mountings": sorted(new_mountings.keys() - old_mountings.keys()),
        "removed_verified_mountings": sorted(
            old_mountings.keys() - new_mountings.keys()
        ),
        "changed_verified_mountings": sorted(
            key
            for key in old_mountings.keys() & new_mountings.keys()
            if old_mountings[key] != new_mountings[key]
        ),
    }


def _diff_for_generation(before: _CatalogData, after: _CatalogData) -> dict:
    missing = {
        name: {
            status: ids
            for status, ids in grouping.items()
            if status not in ("available", "partial")
        }
        for name, grouping in _capabilities(after["parts"]).items()
    }
    return diff_catalogs(before, after) | {
        "status_counts": after["status_counts"],
        "unavailable_ids": missing,
    }


def _digest(value: object) -> str:
    return sha256(_dump(value).encode("utf-8")).hexdigest()


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _render(data: _CatalogData) -> str:
    # One part per line to keep same-version changes reviewable in Git.
    header = {
        "schema": data["schema"],
        "version": data["version"],
        "inputs": data["inputs"],
        "status_counts": data["status_counts"],
        "mountings": data["mountings"],
    }
    rows = ",\n".join(
        f"    {_dump(part_id)}: {_dump(row)}"
        for part_id, row in sorted(data["parts"].items())
    )
    return json.dumps(header, ensure_ascii=False, sort_keys=True, indent=2)[:-2] + (
        f',\n  "parts": {{\n{rows}\n  }}\n}}\n'
    )


def _write(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.close()
            temporary.chmod(S_IMODE(path.stat().st_mode) if path.exists() else 0o644)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _load(path: Path) -> _CatalogData:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ldraw", help="root of the frozen LDraw 2026-10 library")
    parser.add_argument("--output", type=Path, default=_OUTPUT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="check without writing the index"
    )
    mode.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"))
    parser.add_argument(
        "--report", type=Path, help="write the detailed report to this file"
    )
    args = parser.parse_args(argv)
    input_paths = args.compare or [args.output]
    if args.report is not None and args.report.resolve() in {
        path.resolve() for path in input_paths
    }:
        parser.error("--report must be a separate file from the catalog artifacts")
    if args.compare:
        report = diff_catalogs(*map(_load, args.compare))
        content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.report is None:
            print(content, end="")
        else:
            _write(args.report, content)
        return 0

    try:
        generated = build_catalog(Library.find(args.ldraw).root)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    before = (
        _load(args.output)
        if args.output.is_file()
        else {
            "version": "",
            "schema": _CATALOG_SCHEMA,
            "parts": {},
            "inputs": {},
            "status_counts": {},
            "mountings": {},
        }
    )
    report = _diff_for_generation(before, generated)
    if args.report is not None:
        _write(args.report, json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    rendered = _render(generated)
    current = args.output.read_bytes() if args.output.is_file() else None
    up_to_date = current == rendered.encode("utf-8")
    print(f"LDraw {OFFICIAL_PARTS_VERSION}: {len(generated['parts']):,} official IDs")
    print(json.dumps(generated["status_counts"], sort_keys=True))
    print(
        f"{len(report['added_ids']):,} added, {len(report['removed_ids']):,} removed, "
        f"{len(report['changed_parts']):,} changed, "
        f"{len(report['newly_constructible']):,} newly constructible, "
        f"{len(report['newly_verified_mountings']):,} newly verified mountings."
    )
    if args.check:
        print(
            "Catalog is up to date."
            if up_to_date
            else "Catalog differs from the generated data."
        )
        return 0 if up_to_date else 1
    if not up_to_date:
        _write(args.output, rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
