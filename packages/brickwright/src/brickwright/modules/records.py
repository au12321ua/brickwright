"""A child record's checked contract, exported snapshots, and worker sessions."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any

from bricklayer.geometry import Vec3
from bricklayer.ldraw.conversion import LDRAW_TO_STUD, LDrawVec
from pydantic import BaseModel, ConfigDict, Field

from ..record import Generation, Meta, Record, Snapshot, Status
from ..tools.diagnostics import Diagnostic, exception_diagnostic
from .contracts import ModuleError, digest, json_bytes, read_contracts
from .geometry import self_check

if TYPE_CHECKING:
    from bricklayer import LegoBuild

    from ..config import Config

MODULE_DIR = "modules"
CONTRACT_FILE = "module-contract.json"
SESSION_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,128}")
SHA256 = r"^[0-9a-f]{64}$"


class CheckedSnapshot(BaseModel):
    """Immutable identities carried by a successful child self-check."""

    model_config = ConfigDict(frozen=True)

    module_id: str
    snapshot_id: str = Field(pattern=r"^[0-9]+$")
    source_sha256: str = Field(pattern=SHA256)
    report_sha256: str = Field(pattern=SHA256)
    contract_sha256: str = Field(pattern=SHA256)
    mpd_sha256: str = Field(pattern=SHA256)


def atomic_write(path: Path, contents: bytes, *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(contents)
            file.close()
            if immutable:
                try:
                    path.hardlink_to(temporary)
                except FileExistsError:
                    if path.read_bytes() != contents:
                        raise ModuleError(
                            f"Saved artifact digest mismatch: {path.name}."
                        ) from None
            else:
                temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def child_ref(child: Record) -> str:
    return f"{child.path.parent.parent.name}/{MODULE_DIR}/{child.id}"


def init_module(
    parent: Record,
    module_id: str,
    config: Config,
    *,
    prompt: str = "",
    external: bool = True,
) -> Record:
    """Initialize a module, or refresh its contract while preserving the local work."""
    if (parent.path / CONTRACT_FILE).is_file():
        raise ModuleError("Module records cannot create nested module records.")
    bound = read_contracts(parent.path).bound_contract(module_id)
    module_root = parent.path / MODULE_DIR
    module_root.mkdir(exist_ok=True)
    child = Record(module_root / module_id)
    try:
        child.path.mkdir()
    except FileExistsError:
        if not child.meta_path.is_file():
            raise ModuleError(
                f"Module {module_id} is being created by another session."
            )
        contract = child.path / CONTRACT_FILE
        if not contract.is_file() or contract.read_bytes() != bound:
            child.clear_exports()
            child.report_path.unlink(missing_ok=True)
            child.update_meta(status=Status.CREATED, validated_source_sha256=None)
            atomic_write(contract, bound)
        if prompt:
            child.update_meta(prompt=prompt)
        return child

    child.out_dir.mkdir()
    atomic_write(child.path / CONTRACT_FILE, bound)
    header = "\n".join(f"# {line}" for line in _local_summary(child).splitlines())
    child.write_build(
        f'"""{module_id}: a module in local BrickLayer coordinates."""\n\n'
        f"{header}\nimport bricklayer as bl\nfrom brickwright import TestContext\n\n\n"
        "def create_build() -> bl.LegoBuild:\n"
        "    build = bl.LegoBuild()\n"
        "    return build\n\n\n"
        "def run_tests():\n"
        "    tests = TestContext(create_build())\n"
        "    tests.check_connected()\n"
        "    return tests.report()\n"
    )
    _copy_target(parent, child)
    child.write_meta(
        Meta(
            id=module_id,
            created=datetime.now(UTC),
            generation=Generation(
                model=config.model,
                effort=config.effort,
                external=external,
                config=config.model_dump(mode="json", exclude={"records_dir"}),
            ),
            prompt=prompt,
            prompt_image=parent.read_meta().prompt_image,
        )
    )
    return child


def _copy_target(parent: Record, child: Record) -> None:
    target = parent.prompt_image_path
    if target is not None:
        atomic_write(child.path / target.name, target.read_bytes())


def _local_summary(child: Record) -> str:
    contracts = read_contracts(child.path.parent.parent)
    module = contracts.module(child.id)
    inverse = module.pose.inverse
    lines = [
        "All declared ranges and coordinates are in LDraw LDU; negative Y is up.",
        f"World occupancy: {module.occupancy.min} to {module.occupancy.max}",
        f"World origin: {module.origin}; rotation (rows): {module.rotation}",
        f"Colors (LDraw IDs): {list(module.palette)}; maximum parts: {module.part_budget}",
    ]
    for interface in contracts.for_module(child.id):
        side = interface.modules.index(child.id)
        for number, joint in enumerate(interface.joints, start=1):
            socket = joint.endpoint(side)
            position = inverse.apply(LDrawVec(*socket.position).to_position())
            axis = inverse.rotation @ (LDRAW_TO_STUD @ Vec3(*socket.axis))
            lines.append(
                f"{interface.id}/{number}, {socket.role}: {socket.part_id} {socket.type}, "
                f"local position {tuple(round(float(value), 6) for value in position)} "
                f"and axis {tuple(round(float(value), 6) for value in axis)}. "
                "Local position uses BrickLayer (studs, studs, bricks)."
            )
    return "\n".join(lines)


def module_instructions(child: Record, *, session_id: str | None = None) -> str:
    """Give an independent worker its editable path and a verifiable handoff."""
    bound = _require_current_contract(child)
    reference = child_ref(child)
    if session_id is not None:
        _start_session(child, session_id, digest(bound))
    steps = [
        f"Build module {child.id} in {child.build_path.resolve()}.",
        "Edit only this module record. create_build() returns a LegoBuild in local coordinates.",
        _local_summary(child),
        (
            "Keep each declared endpoint free for its mating module. Only its declared "
            "mating geometry may pass the occupancy face."
        ),
        f"Run brickwright external validate {reference} and brickwright external finalize {reference}.",
        (
            f"Run brickwright external module finish {reference} --session-id {session_id} "
            "and return its session ID, snapshot ID, and self-check result."
            if session_id is not None
            else "Return the module record ID and its self-check result."
        ),
    ]
    return "\n\n".join(steps) + "\n"


def _require_current_contract(child: Record) -> bytes:
    child_path = child.path
    try:
        current = read_contracts(child_path.parent.parent).bound_contract(child.id)
        if (
            child_path.parent.name != MODULE_DIR
            or (child_path / CONTRACT_FILE).read_bytes() != current
        ):
            raise ValueError("saved contract differs from the parent plan")
        return current
    except (OSError, ValueError) as exc:
        raise ModuleError(
            f"Module {child.id} needs its current contract: {exc}",
            evidence={"module": child.id},
            next_action=f"Run brickwright external module init {child_path.parent.parent.name} {child.id}.",
        ) from exc


def check_module(
    build: LegoBuild, record_path: Path
) -> tuple[str | None, list[Diagnostic]]:
    """Return the checked contract digest and possible module-only diagnostics."""
    if (
        record_path.parent.name != MODULE_DIR
        or not (record_path.parent.parent / "meta.json").is_file()
    ):
        return None, []
    try:
        bound = _require_current_contract(Record(record_path))
        contracts = read_contracts(record_path.parent.parent)
        if contracts.bound_contract(record_path.name) != bound:
            raise ModuleError(
                f"Module {record_path.name} changed contract during validation."
            )
    except ModuleError as exc:
        return None, [exception_diagnostic(exc, "module_validate")]
    return digest(bound), self_check(
        build,
        contracts.module(record_path.name),
        contracts.for_module(record_path.name),
    )


def selected_snapshot(child: Record, contract_bytes: bytes) -> CheckedSnapshot:
    """Freeze the newest successful snapshot matching the current finalized source."""
    contract_sha256 = digest(contract_bytes)
    detail: dict[str, Any] = {"module": child.id}
    failure = ModuleError(
        f"Module {child.id} must pass self-check and finalize against the current contract.",
        evidence=detail,
        next_action=(
            f"Run brickwright external module init {child.path.parent.parent.name} {child.id}, "
            f"then brickwright external validate {child_ref(child)} and "
            f"brickwright external finalize {child_ref(child)}."
        ),
    )
    try:
        meta = child.read_meta()
        if (
            child.path / CONTRACT_FILE
        ).read_bytes() != contract_bytes or not meta.finalized:
            raise failure
        mpd = child.ldr_path.read_bytes()
        mpd_sha256 = digest(mpd)
    except (OSError, json.JSONDecodeError) as exc:
        raise failure from exc

    for snapshot in reversed(child.snapshots):
        try:
            report_bytes = snapshot.report_path.read_bytes()
            report = json.loads(report_bytes)
            if not _matches_report(
                report, meta.exported_source_sha256, contract_sha256, mpd_sha256
            ):
                continue
            source_sha256 = digest(snapshot.build_path.read_bytes())
        except (OSError, ValueError):
            continue
        if not isinstance(report.get("module_motion"), dict):
            raise ModuleError(
                f"Module {child.id} needs a new validation and finalization to attest its declared motion.",
            )
        if source_sha256 != meta.exported_source_sha256:
            raise ModuleError(
                f"Source digest mismatch for module {child.id}, snapshot {snapshot.name}."
            )
        atomic_write(snapshot.path / "module.ldr", mpd, immutable=True)
        return CheckedSnapshot(
            module_id=child.id,
            snapshot_id=snapshot.name,
            source_sha256=source_sha256,
            report_sha256=digest(report_bytes),
            contract_sha256=contract_sha256,
            mpd_sha256=mpd_sha256,
        )
    raise failure


def _matches_report(
    report: Any, source_sha: str | None, contract_sha: str, mpd_sha: str
) -> bool:
    return (
        isinstance(report, dict)
        and report.get("ok") is True
        and report.get("source_sha256") == source_sha
        and report.get("module_contract_sha256") == contract_sha
        and report.get("module_mpd_sha256") == mpd_sha
    )


def snapshot_artifacts(
    child: Record, selection: CheckedSnapshot
) -> tuple[bytes, bytes | None]:
    snapshot = Snapshot(child.snapshots_dir / selection.snapshot_id)
    try:
        report_bytes = snapshot.report_path.read_bytes()
        source = snapshot.build_path.read_bytes()
        mpd = (snapshot.path / "module.ldr").read_bytes()
    except OSError as exc:
        raise ModuleError(
            f"Missing saved content for module {child.id}/{snapshot.name}."
        ) from exc
    if (
        digest(report_bytes) != selection.report_sha256
        or digest(source) != selection.source_sha256
        or digest(mpd) != selection.mpd_sha256
        or not _matches_report(
            json.loads(report_bytes),
            selection.source_sha256,
            selection.contract_sha256,
            selection.mpd_sha256,
        )
    ):
        raise ModuleError(
            f"Snapshot digest or self-check mismatch for module {child.id}/{snapshot.name}."
        )
    motion = json.loads(report_bytes).get("module_motion")
    return mpd, json_bytes(motion) if isinstance(motion, dict) else None


def _session_path(child: Record, session_id: str) -> Path:
    if not SESSION_PATTERN.fullmatch(session_id):
        raise ModuleError(
            "Session ID must contain only letters, digits, underscores, or hyphens."
        )
    return child.path / "sessions" / f"{session_id}.json"


def _start_session(child: Record, session_id: str, contract_sha: str) -> None:
    path = _session_path(child, session_id)
    contents = json_bytes(
        {
            "session_id": session_id,
            "record_id": child_ref(child),
            "started_at": datetime.now(UTC).isoformat(),
            "contract_sha256": contract_sha,
            "validation_snapshots_at_start": len(child.snapshots),
        }
    )
    if not path.exists():
        atomic_write(path, contents)


def finish_module_session(
    child: Record, session_id: str, *, usage: dict | None = None
) -> dict:
    """Record one independent session, its elapsed time, and its verified handoff."""
    path = _session_path(child, session_id)
    try:
        result = json.loads(path.read_bytes())
    except FileNotFoundError as exc:
        raise ModuleError(
            f"Module session {session_id!r} must start with module instructions."
        ) from exc
    if result.get("finished_at"):
        return result
    finished = datetime.now(UTC)
    start = datetime.fromisoformat(result["started_at"])
    result.update(
        finished_at=finished.isoformat(),
        elapsed_seconds=(finished - start).total_seconds(),
        validation_snapshots=len(child.snapshots)
        - result["validation_snapshots_at_start"],
        self_check_passed=False,
        snapshot_id=None,
    )
    with suppress(ModuleError):
        checked = selected_snapshot(child, _require_current_contract(child))
        if (
            checked.contract_sha256 == result["contract_sha256"]
            and result["validation_snapshots"] > 0
        ):
            result.update(
                self_check_passed=True,
                snapshot_id=checked.snapshot_id,
                source_sha256=checked.source_sha256,
                report_sha256=checked.report_sha256,
            )
    if usage is not None:
        result["usage"] = usage
        if child.messages_path.is_file():
            transcript = path.parent / f"{session_id}.messages.jsonl"
            atomic_write(transcript, child.messages_path.read_bytes(), immutable=True)
            result["transcript"] = str(transcript.relative_to(child.path))
    atomic_write(path, json_bytes(result))
    return result
