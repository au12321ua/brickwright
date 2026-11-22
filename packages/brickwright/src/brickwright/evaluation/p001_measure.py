"""Replay a finished build in its native checkout and trace placement provenance."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bricklayer import Element, LegoBuild, Placement, flatten, to_ldraw
from bricklayer.modeling import transforms
from bricklayer.modeling.placement import leaf_paths

from brickwright.tools.validation import build_and_validate

# These flags travel with a Placement when the SDK copies it. Keeping provenance
# on the placed occurrence avoids marking grid placements of the same content.
_DIRECT = 1
_ASSEMBLY = 2
_ORIGIN = "_p001_evaluation_connector_origin"


def _origin(placement: Placement) -> int:
    return getattr(placement, _ORIGIN, 0)


def _mark(placement: Placement, origin: int) -> None:
    if origin:
        object.__setattr__(placement, _ORIGIN, _origin(placement) | origin)


def _path_origin(path: tuple[Placement, ...]) -> int:
    origin = 0
    for placement in path:
        origin |= _origin(placement)
    return origin


@contextmanager
def trace_placements() -> Generator[None]:
    """Observe successful connector placements and the SDK's transformations."""
    transform = Placement.transformed
    compose = LegoBuild._compose
    posed = LegoBuild.posed
    rebuild = transforms._rebuild

    def trace_mount(place: Callable[..., Placement]) -> Callable[..., Placement]:
        def traced(build: LegoBuild, *args, **kwargs) -> Placement:
            placement = place(build, *args, **kwargs)
            _mark(
                placement,
                _DIRECT if isinstance(placement.content, Element) else _ASSEMBLY,
            )
            return placement

        return traced

    def traced_transform(placement: Placement, pose) -> Placement:
        new = transform(placement, pose)
        _mark(new, _origin(placement))
        return new

    def traced_compose(
        build: LegoBuild, outer: Placement, inner: Placement
    ) -> Placement:
        # flatten() can discard the outer placement of a connected subassembly.
        new = compose(build, outer, inner)
        _mark(new, _origin(outer) | _origin(inner))
        return new

    def traced_pose(build: LegoBuild, positions) -> LegoBuild:
        new = posed(build, positions)
        for path, replacement in zip(
            leaf_paths(build.placements), new.placements, strict=True
        ):
            _mark(replacement, _path_origin(path))
        return new

    def traced_rebuild(build: LegoBuild, transform, **kwargs) -> LegoBuild:
        # This also covers mirrors that create a handed replacement part.
        new = rebuild(build, transform, **kwargs)
        for original, replacement in zip(build.placements, new.placements, strict=True):
            _mark(replacement, _origin(original))
        return new

    with (
        ExitStack() as placement_hooks,
        patch.object(Placement, "transformed", traced_transform),
        patch.object(LegoBuild, "_compose", traced_compose),
        patch.object(LegoBuild, "posed", traced_pose),
        patch.object(transforms, "_rebuild", traced_rebuild),
    ):
        for name in ("connect", "place_connection"):
            if (place := getattr(LegoBuild, name, None)) is not None:
                placement_hooks.enter_context(
                    patch.object(LegoBuild, name, trace_mount(place))
                )
        yield


def count_placements(build: LegoBuild) -> dict[str, int]:
    paths = tuple(leaf_paths(build.placements))
    origins = tuple(_path_origin(path) for path in paths)
    live_joint_children = {joint.child.path for joint in build.flattened_joints}
    return {
        "pieces": len(paths),
        "connector_placed_pieces": sum(bool(origin) for origin in origins),
        "direct_connector_pieces": sum(bool(origin & _DIRECT) for origin in origins),
        "subassembly_connector_pieces": sum(
            bool(origin & _ASSEMBLY) for origin in origins
        ),
        "live_joint_child_pieces": sum(path in live_joint_children for path in paths),
    }


def _motion_evidence(build: LegoBuild, assets: Path) -> dict[str, Any] | None:
    mobile = [joint for joint in build.flattened_joints if joint.is_mobile]
    if not mobile:
        return None
    try:
        report = build.check_motion(samples=5, max_samples=256)
        poses = []
        for index, joint in enumerate(mobile, 1):
            assert joint.motion is not None
            for bound, coordinates in (
                ("lower", joint.motion.lower),
                ("upper", joint.motion.upper),
            ):
                pose = build.posed({joint: coordinates})
                name = f"pose-{index:02d}-{bound}.ldr"
                (assets / name).write_text(to_ldraw(pose), encoding="utf-8")
                poses.append({"file": name, "coordinates": asdict(coordinates)})
    except (TypeError, ValueError, RuntimeError):
        return {"status": "sampled motion evidence unavailable"}
    return {
        "samples": report.samples_checked,
        "collisions": len(report.collisions),
        "unsupported_occurrences": len(report.floating),
        "disengagements": len(report.disconnections),
        "poses": poses,
    }


def measure(record: Path, output: Path) -> None:
    source = record / "build.py"
    artifact = record / "out" / "build.ldr"
    snapshot = source.read_bytes()
    ldraw = artifact.read_bytes()
    with trace_placements():
        build, validation = build_and_validate(str(source), snapshot.decode("utf-8"))
    if build is None or not validation.ok:
        raise ValueError("replaying the source failed validation")
    if to_ldraw(build).encode() != ldraw:
        raise ValueError("replayed source does not produce the final LDraw artifact")
    counts = count_placements(build)
    if counts["pieces"] != validation.brick_count:
        raise ValueError("replayed placement count differs from native validation")
    output.mkdir()
    assets = output / "assets"
    assets.mkdir()
    # The replay has already matched the archived artifact. Flattening the review
    # copy strips agent-assigned submodel names and construction step metadata.
    (assets / "model.ldr").write_text(to_ldraw(flatten(build)), encoding="utf-8")
    measurements = {
        "source_sha256": sha256(snapshot).hexdigest(),
        "artifact_sha256": sha256(ldraw).hexdigest(),
        **counts,
        "evidence": {
            "pieces": counts["pieces"],
            "dimensions": validation.dimensions.model_dump()
            if validation.dimensions
            else None,
            "components": build.num_connected_components,
            "collision_free_and_supported": True,
            "motion": _motion_evidence(build, assets),
        },
    }
    (output / "measurement.json").write_text(json.dumps(measurements, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    measure(arguments.record, arguments.output)
