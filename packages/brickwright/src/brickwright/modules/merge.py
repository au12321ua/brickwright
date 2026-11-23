"""Freeze and replay the child snapshots used for a parent model."""

from __future__ import annotations

import json
import re
import warnings
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from bricklayer import Element, LegoBuild
from bricklayer.geometry.connections import connectors_mate
from bricklayer.io import LDrawImportWarning, from_ldraw
from bricklayer.ldraw.conversion import pose_to_ldraw
from bricklayer.modeling.placement import Placement, leaf_paths
from pydantic import BaseModel, ConfigDict, JsonValue

from ..record import Record
from ..tools.diagnostics import Diagnostic, exception_diagnostic
from .contracts import Contracts, ModuleError, digest, json_bytes, read_contracts
from .geometry import interface_points, match_endpoint, self_check
from .motion import check_merged_motion, restore_motion
from .records import (
    MODULE_DIR,
    CheckedSnapshot,
    atomic_write,
    selected_snapshot,
    snapshot_artifacts,
)

if TYPE_CHECKING:
    from bricklayer.geometry import Pose

MERGE_FILENAME = "merge-manifest.json"
MERGE_HISTORY = "merge-manifests"


class Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    contracts: Contracts
    selections: tuple[CheckedSnapshot, ...]


@dataclass(frozen=True)
class FrozenMerge:
    """Picklable bytes captured once before build, validation, render, or export."""

    manifest_bytes: bytes
    modules: tuple[tuple[str, bytes], ...]
    motions: tuple[tuple[str, bytes | None], ...] = ()

    @property
    def manifest_sha256(self) -> str:
        return digest(self.manifest_bytes)

    @property
    def manifest(self) -> Manifest:
        return Manifest.model_validate_json(self.manifest_bytes)


_active: ContextVar[FrozenMerge | None] = ContextVar(
    "brickwright_frozen_merge", default=None
)


def prepare_merge(record: Record) -> FrozenMerge | None:
    """Atomically publish a complete selection of current, checked children."""
    contracts = read_contracts(record.path)
    path = record.path / MERGE_FILENAME
    if not contracts.modules:
        path.unlink(missing_ok=True)
        return None
    selected = tuple(
        selected_snapshot(
            Record(record.path / MODULE_DIR / module.id),
            contracts.bound_contract(module.id),
        )
        for module in contracts.modules
    )
    contents = json_bytes(
        Manifest(contracts=contracts, selections=selected).model_dump(mode="json")
    )
    frozen = _load(record, contents)
    if read_contracts(record.path) != contracts:
        raise ModuleError(
            "Module contracts changed while freezing the merge. Retry the parent operation."
        )
    atomic_write(
        record.path / MERGE_HISTORY / f"{frozen.manifest_sha256}.json",
        contents,
        immutable=True,
    )
    atomic_write(path, contents)
    return frozen


def load_merge(record: Record, manifest_sha256: str | None = None) -> FrozenMerge:
    """Replay the selected snapshots, optionally using an archived manifest."""
    path = record.path / MERGE_FILENAME
    if manifest_sha256 is not None:
        if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
            raise ModuleError("Invalid merge manifest digest.")
        path = record.path / MERGE_HISTORY / f"{manifest_sha256}.json"
    try:
        contents = path.read_bytes()
    except OSError as exc:
        raise ModuleError(f"Missing merge manifest for {record.id}.") from exc
    if manifest_sha256 is not None and digest(contents) != manifest_sha256:
        raise ModuleError("Merge manifest digest does not match its archived identity.")
    return _load(record, contents)


def _load(record: Record, contents: bytes) -> FrozenMerge:
    try:
        manifest = Manifest.model_validate_json(contents)
    except ValueError as exc:
        raise ModuleError(f"Invalid merge manifest: {exc}") from exc
    if manifest.schema_version != 1 or tuple(
        item.module_id for item in manifest.selections
    ) != tuple(item.id for item in manifest.contracts.modules):
        raise ModuleError(
            "The merge manifest does not select each declared module exactly once."
        )
    mpds = []
    motions = []
    for selection in manifest.selections:
        module = selection.module_id
        if (
            digest(manifest.contracts.bound_contract(module))
            != selection.contract_sha256
        ):
            raise ModuleError(f"Contract digest mismatch for selected module {module}.")
        child = Record(record.path / MODULE_DIR / module)
        mpd, motion = snapshot_artifacts(child, selection)
        mpds.append((module, mpd))
        motions.append((module, motion))
    return FrozenMerge(contents, tuple(mpds), tuple(motions))


def merge_state(record: Record) -> tuple[bool, list[Diagnostic]]:
    """Check whether the sealed merge still matches the current module plan."""
    try:
        contracts = read_contracts(record.path)
        if not contracts.modules and not (record.path / MERGE_FILENAME).exists():
            return True, []
        merge = load_merge(record)
        if any(motion is None for _, motion in merge.motions):
            raise ModuleError(
                "This merge needs revalidated snapshots with motion attestation.",
                code="module.articulation_unsupported",
            )
        if merge.manifest.contracts != contracts:
            raise ModuleError(
                "The merge contract is obsolete. Prepare a new merge against the current plan."
            )
    except ModuleError as exc:
        return False, [exception_diagnostic(exc, "module_merge")]
    return True, []


@contextmanager
def merge_context(merge: FrozenMerge | None) -> Generator[None]:
    token = _active.set(merge)
    try:
        yield
    finally:
        _active.reset(token)


def _read_module(module: str, mpd: bytes) -> LegoBuild:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", LDrawImportWarning)
            return from_ldraw(mpd.decode())
    except (ValueError, LDrawImportWarning) as exc:
        raise ModuleError(
            f"Cannot reread sealed MPD for module {module}: {exc}"
        ) from exc


def place_module(build: LegoBuild, module_id: str) -> Placement:
    """Place the selected independent module at its declared world transform."""
    merge = _active.get()
    if merge is None:
        raise ModuleError(
            "place_module requires a frozen merge. Run a BrickWright record command.",
            next_action="Initialize, validate and finalize the modules, then validate the parent record.",
        )
    module = merge.manifest.contracts.module(module_id)
    mpd = dict(merge.modules).get(module_id)
    if mpd is None or module_id in build.names:
        raise ModuleError(f"Expected exactly one placement of module {module_id}.")
    content = _read_module(module_id, mpd)
    motion = dict(merge.motions).get(module_id)
    if motion is not None:
        content = restore_motion(content, motion)
    return build.add_raw(content, module.pose, name=module_id)


def module_owners(build: LegoBuild, merge: FrozenMerge) -> tuple[str | None, ...]:
    known = {name for name, _ in merge.modules}
    names = {
        placement: name
        for name, placement in build.names.items()
        if name in known and isinstance(placement.content, LegoBuild)
    }
    return tuple(names.get(path[0]) for path in leaf_paths(build.placements))


def merge_evidence(
    build: LegoBuild, merge: FrozenMerge
) -> tuple[list[dict[str, JsonValue]], list[Diagnostic]]:
    """Resolve the exact endpoint pairs on the native or standard-reread model."""
    manifest = merge.manifest
    contracts = manifest.contracts
    module_mpds = dict(merge.modules)
    diagnostics = []
    points = {}
    for module in contracts.modules:
        placement = build.names.get(module.id)
        if (
            placement is None
            or not isinstance(placement.content, LegoBuild)
            or sum(candidate is placement for candidate in build.placements) != 1
            or not _same_pose(module.pose, placement.pose)
            or not _same_model(
                _read_module(module.id, module_mpds[module.id]), placement.content
            )
        ):
            diagnostics.append(
                Diagnostic(
                    code="module.contract_stale",
                    stage="module_merge",
                    message=f"The merged model is missing the exact sealed placement of {module.id}.",
                    evidence={"module": module.id},
                    next_action=f"Place the module exactly once using place_module(build, {module.id!r}).",
                )
            )
            continue
        diagnostics.extend(
            self_check(placement.content, module, contracts.for_module(module.id))
        )
        points[module.id] = interface_points(
            [
                leaf.transformed(placement.pose)
                for leaf in placement.content.flattened_placements
            ]
        )

    evidence: list[dict[str, JsonValue]] = []
    for interface in contracts.interfaces:
        a, b = interface.modules
        if a not in points or b not in points:
            continue
        for number, joint in enumerate(interface.joints, start=1):
            left, left_evidence = match_endpoint(points[a], joint.a)
            right, right_evidence = match_endpoint(points[b], joint.b)
            shared: dict[str, JsonValue] = {"interface": interface.id, "joint": number}
            if (
                left is None
                or right is None
                or not connectors_mate(left.connector, right.connector)
            ):
                diagnostics.append(
                    Diagnostic(
                        code="module.interface_missing",
                        stage="module_merge",
                        message=f"The exact endpoints of {interface.id}/{number} do not mate.",
                        evidence=shared
                        | {"a": left_evidence, "b": right_evidence, "modules": [a, b]},
                        next_action="Correct the two declared endpoints and re-finalize affected modules.",
                    )
                )
                continue
            evidence.append(
                shared
                | {
                    "a": left.evidence(joint.a.role) | {"module": a},
                    "b": right.evidence(joint.b.role) | {"module": b},
                }
            )

    owners = dict(zip(build.flattened_placements, module_owners(build, merge)))
    for first, second in build.colliding_placements:
        a, b = owners[first], owners[second]
        if a != b and (a is not None or b is not None):
            diagnostics.append(
                Diagnostic(
                    code="module.collision",
                    stage="module_merge",
                    message="Parts from different module ownership groups collide.",
                    evidence={
                        "modules": [a, b],
                        "part_ids": [first.content.ldraw_id, second.content.ldraw_id],
                    },
                )
            )
    motions = {module: data for module, data in merge.motions if data is not None}
    unattested = [module for module, data in merge.motions if data is None]
    if unattested:
        diagnostics.append(
            Diagnostic(
                code="module.articulation_unsupported",
                stage="module_merge",
                message="The archived static module MPDs lack verified motion attestation.",
                evidence={"modules": unattested},
                next_action="Validate and finalize the affected modules before making a new merge.",
            )
        )
    try:
        motion_evidence = check_merged_motion(build, motions)
    except ModuleError as exc:
        diagnostics.append(exception_diagnostic(exc, "module_merge"))
    else:
        if motion_evidence is not None:
            evidence.append({"motion": motion_evidence})
    return evidence, diagnostics


def verify_merge(build: LegoBuild, merge: FrozenMerge) -> list[Diagnostic]:
    return merge_evidence(build, merge)[1]


def _same_model(expected: LegoBuild, actual: LegoBuild) -> bool:
    if expected.step_markers != actual.step_markers or len(expected.placements) != len(
        actual.placements
    ):
        return False
    for a, b in zip(expected.placements, actual.placements):
        if not _same_pose(a.pose, b.pose):
            return False
        if isinstance(a.content, Element) and isinstance(b.content, Element):
            if (a.content.ldraw_id, a.content.color.ldraw_id) != (
                b.content.ldraw_id,
                b.content.color.ldraw_id,
            ):
                return False
        elif isinstance(a.content, LegoBuild) and isinstance(b.content, LegoBuild):
            if not _same_model(a.content, b.content):
                return False
        else:
            return False
    return True


def _same_pose(left: Pose, right: Pose) -> bool:
    return all(
        np.allclose(a, b, atol=1e-5, rtol=0)
        for a, b in zip(pose_to_ldraw(left), pose_to_ldraw(right))
    )


def module_status(parent: Record) -> dict:
    """List each module's current state and its distinction from a frozen choice."""
    contracts = read_contracts(parent.path)
    path = parent.path / MERGE_FILENAME
    manifest = (
        Manifest.model_validate_json(path.read_bytes()) if path.is_file() else None
    )
    choices = {item.module_id: item for item in manifest.selections} if manifest else {}
    modules = []
    for module in contracts.modules:
        child = Record(parent.path / MODULE_DIR / module.id)
        bound = contracts.bound_contract(module.id)
        saved = child.path / "module-contract.json"
        contract_stale = saved.is_file() and saved.read_bytes() != bound
        meta = child.read_meta() if child.meta_path.is_file() else None
        sessions = [
            json.loads(path.read_bytes())
            for path in sorted((child.path / "sessions").glob("*.json"))
        ]
        chosen = choices.get(module.id)
        source = child.source_sha256 if child.build_path.is_file() else None
        modules.append(
            {
                "id": module.id,
                "record_id": f"{parent.id}/{MODULE_DIR}/{module.id}",
                "status": meta.status.value if meta else "uninitialized",
                "finalized": bool(meta and meta.finalized and not contract_stale),
                "contract_stale": contract_stale,
                "contract_sha256": digest(bound),
                "source_sha256": source,
                "selected_snapshot": chosen.snapshot_id if chosen else None,
                "has_update": bool(
                    chosen
                    and (
                        source != chosen.source_sha256
                        or digest(bound) != chosen.contract_sha256
                    )
                ),
                "sessions": sessions,
                "elapsed_seconds": sum(
                    item.get("elapsed_seconds", 0) for item in sessions
                ),
            }
        )
    return {
        "record_id": parent.id,
        "modules": modules,
        "merge_sha256": digest(path.read_bytes()) if manifest else None,
    }
