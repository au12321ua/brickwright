"""Regenerate the P-003 supplement from the official LDraw 2026-10 release.

Updates the targeted records in an existing BrickLayer data directory. This
does not need the original BrickNet PLY dump or touch any BrickNet mesh frame.
"""

import argparse
import json
import tempfile
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from ...geometry.collision import SURFACE_INSET_LDU
from ..conversion import pose_from_type1, pose_to_ldraw
from ..meshes import DATA_DIR, encode_mesh
from .extract_part_geometry import part_bbox
from .library import Library, write_parts_json
from .official_mesh import CELL_LDU, official_mesh

_HOOK_BASE = "2650"
_HOOK_ARM = "2651"
_HOOK_SHORTCUTS = ("2651c01-f1", "2651c01-f2")
_SINGLE_FINGER = "53923"
_DUAL_FINGER = "30553"
_HINGE_CATEGORY = "Hinge"
_FRAME_NAME = f"{_SINGLE_FINGER}.drc"
_MANIFEST = "official_part_supplements.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ldraw", help="path to the official LDraw library")
    parser.add_argument(
        "--output", type=Path, default=DATA_DIR, help="existing data directory"
    )
    parser.add_argument(
        "--check", action="store_true", help="check the committed supplement"
    )
    args = parser.parse_args(argv)

    lib = Library.find(args.ldraw)
    frame, manifest = generated_supplement(lib)
    bbox = part_bbox(lib, _SINGLE_FINGER)
    if bbox is None:
        parser.error(f"No measured bounds for {_SINGLE_FINGER}")
    description = lib.description(_SINGLE_FINGER)
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    output = args.output
    bounds_path = output / "part_geometry.json"
    taxonomy_path = output / "part_taxonomy.json"
    index_path = output / "meshes" / "index.json"
    archive_path = output / "meshes" / f"{_HINGE_CATEGORY}.zip"
    manifest_path = output / _MANIFEST
    bounds = json.loads(bounds_path.read_text())
    taxonomy = json.loads(taxonomy_path.read_text())
    index = json.loads(index_path.read_text())
    expected_bounds = {"bbox": [list(corner) for corner in bbox]}

    if args.check:
        with ZipFile(archive_path) as archive:
            archived_frame = (
                archive.read(_FRAME_NAME) if _FRAME_NAME in archive.namelist() else None
            )
        checks = (
            (bounds_path, bounds["parts"].get(_SINGLE_FINGER) == expected_bounds),
            (taxonomy_path, taxonomy["parts"].get(_SINGLE_FINGER) == description),
            (index_path, index.get(_SINGLE_FINGER) == _HINGE_CATEGORY),
            (archive_path, archived_frame == frame),
            (
                manifest_path,
                manifest_path.is_file() and manifest_path.read_text() == manifest_text,
            ),
        )
        updated = [path.name for path, same in checks if not same]
        if updated:
            print("Official supplement differs in " + ", ".join(updated))
            return 1
        print("Official collision mesh, reference poses and provenance are up to date.")
        return 0

    _replace_frame(archive_path, frame)
    bounds["parts"][_SINGLE_FINGER] = expected_bounds
    taxonomy["parts"][_SINGLE_FINGER] = description
    index[_SINGLE_FINGER] = _HINGE_CATEGORY
    for path, rows in ((bounds_path, bounds), (taxonomy_path, taxonomy)):
        write_parts_json(
            path,
            rows["parts"],
            generated_by=rows["_generated_by"],
            note=rows["_note"],
            units=rows.get("units"),
        )
    index_path.write_text(json.dumps(index, sort_keys=True, indent=0))
    manifest_path.write_text(manifest_text)
    print(f"Wrote official supplement to {output}.")
    return 0


def generated_supplement(lib: Library) -> tuple[bytes, dict[str, object]]:
    mesh, inputs = official_mesh(lib, _SINGLE_FINGER)
    frame = encode_mesh(mesh)
    shortcut_poses = {
        part_id: _shortcut_reference(lib, part_id) for part_id in _HOOK_SHORTCUTS
    }
    input_paths = {path.relative_to(lib.root).as_posix() for path in inputs}
    input_paths.update(
        f"parts/{part_id}.dat"
        for part_id in (_HOOK_BASE, _HOOK_ARM, _DUAL_FINGER, *_HOOK_SHORTCUTS)
    )
    input_paths.update(("p/clh5.dat", "p/clh5s.dat", "parts/s/480s02.dat"))
    files = {name: _source_file(lib.root / name) for name in sorted(input_paths)}
    hook_sources = [f"parts/{part_id}.dat" for part_id in _HOOK_SHORTCUTS]
    parts = {
        _HOOK_BASE: {
            "connectors": {
                "hook_slide_channel": {
                    "files": [f"parts/{_HOOK_BASE}.dat", *hook_sources],
                    "method": "Measured in the channel on its local +X rail axis. The reference pose is halfway between both shortcuts.",
                },
            },
        },
        _HOOK_ARM: {
            "connectors": {
                "hook_slide_arm": {
                    "files": [f"parts/{_HOOK_ARM}.dat", *hook_sources],
                    "method": "Measured in the tab on its local +X rail axis.",
                },
            },
        },
        _SINGLE_FINGER: {
            "collider": {
                "files": sorted(
                    path.relative_to(lib.root).as_posix() for path in inputs
                ),
                "method": f"Inset occupancy surface at {CELL_LDU:g} LDU resolution from the official triangles.",
                "inset_ldu": SURFACE_INSET_LDU,
                "voxel_size_ldu": CELL_LDU,
                "frame_sha256": sha256(frame).hexdigest(),
                "source_to_part_ldraw": [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1],
            },
            "connectors": {
                "axle_hole": {
                    "files": [f"parts/{_SINGLE_FINGER}.dat", "p/axl3hole.dat"],
                    "method": "Measured the axle hole center along its 20 LDU axis.",
                },
                "hinge_2_in": {
                    "files": [f"parts/{_SINGLE_FINGER}.dat", "p/clh15.dat"],
                    "method": "Measured the center and axis of the single finger pivot.",
                },
            },
        },
    }
    return frame, {
        "ldraw_version": "2026-10",
        "files": files,
        "parts": parts,
        "shortcut_poses": shortcut_poses,
    }


def _shortcut_reference(lib: Library, part_id: str) -> dict[str, object]:
    path = f"parts/{part_id}.dat"
    lines = (lib.root / path).read_text(encoding="latin-1").splitlines()
    by_part = {}
    for row in map(str.split, lines):
        if row and row[0] == "1":
            by_part[Path(row[14]).stem] = pose_from_type1([float(n) for n in row[2:14]])
    anchor = by_part[_HOOK_BASE]
    arm = by_part[_HOOK_ARM]
    rotation, translation = pose_to_ldraw(anchor.inverse.compose(arm))
    return {
        "source": path,
        "anchor": _HOOK_BASE,
        "moving": _HOOK_ARM,
        "relative_ldraw": [*translation.tolist(), *rotation.flatten().tolist()],
    }


def _source_file(path: Path) -> dict[str, str]:
    content = path.read_bytes()
    lines = content.decode("latin-1").splitlines()
    return {
        "author": next(
            line.removeprefix("0 Author: ").strip()
            for line in lines
            if line.startswith("0 Author: ")
        ),
        "license": next(
            line.removeprefix("0 !LICENSE ").strip()
            for line in lines
            if line.startswith("0 !LICENSE ")
        ),
        "sha256": sha256(content).hexdigest(),
    }


def _replace_frame(path: Path, frame: bytes) -> None:
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        staged = Path(directory) / path.name
        with ZipFile(path) as source, ZipFile(staged, "w", ZIP_STORED) as target:
            old_members = {member.filename: member for member in source.infolist()}
            for name in sorted(old_members.keys() | {_FRAME_NAME}):
                if name == _FRAME_NAME:
                    target.writestr(ZipInfo(_FRAME_NAME), frame)
                else:
                    target.writestr(old_members[name], source.read(name))
        staged.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
