"""Fixed-camera rendering, comparisons, and independent visual review."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from importlib import import_module
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID, uuid4

from PIL import Image
from pydantic import BaseModel, TypeAdapter, ValidationError

from ._aligned_compare import compare_images, read_rgba
from ._aligned_render import Cameras, render_model
from ._review_reports import REVIEW_GUIDANCE, FailedReport, Judgment, Report, Reviewer
from .config import Config
from .record import Record
from .tools.build_execution import load_build_module, run_in_subprocess


class _Asset(BaseModel):
    path: str
    sha256: str

    @classmethod
    def capture(cls, record: Record, path: Path) -> _Asset:
        path = Path(os.path.abspath(path))
        base = Path(os.path.abspath(record.path))
        return cls(
            path=str(path.relative_to(base) if path.is_relative_to(base) else path),
            sha256=_hash(path.read_bytes()),
        )

    def read(self, record: Record) -> bytes:
        data = (record.path / self.path).read_bytes()
        if _hash(data) != self.sha256:
            raise ValueError(f"review input changed: {self.path}")
        return data


class _Render(BaseModel):
    schema_version: int = 1
    snapshot_id: str | None
    source_sha256: str
    model_sha256: str
    cameras: _Asset
    image_pixels: tuple[int, int]
    views: dict[str, _Asset]


class _Comparison(BaseModel):
    schema_version: int = 1
    render: _Asset
    references: dict[str, _Asset]
    overlays: dict[str, _Asset]
    metrics: dict[str, dict[str, Any]]


class _PrivateInputs(BaseModel):
    schema_version: int = 1
    manifest_sha256: str
    snapshot_id: str
    source_sha256: str
    prompt_sha256: str | None
    parent_session_id: str | None
    assets: dict[str, _Asset]


def _structural_state(record: Record) -> dict:
    return import_module("brickwright.flow").flow_status(record, include_review=False)


def _hash(data: bytes) -> str:
    return sha256(data).hexdigest()


def _json(data: Any) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.parent / f".{uuid4().hex}.tmp"
    try:
        stage.write_bytes(data)
        stage.replace(path)
    finally:
        stage.unlink(missing_ok=True)


def _render_path(record: Record) -> Path:
    return record.out_dir / "aligned-render.json"


def _comparison_path(record: Record) -> Path:
    return record.out_dir / "comparison.json"


def _preview_model(record: Record, source: str, destination: Path) -> None:
    from bricklayer import write_ldraw

    build = load_build_module(record.build_path, source).create_build()
    write_ldraw(build, destination)


def _render_worker(
    record: Record, source: str | None, model: Path, spec: Cameras, output: Path
) -> tuple[dict[str, Path], str]:
    if source is not None:
        _preview_model(record, source, model)
    return render_model(model, spec, output), _hash(model.read_bytes())


def render_cameras(record: Record, cameras: Path, config: Config | None = None) -> dict:
    """Render fixed views from the current checked MPD or an unvalidated preview."""
    camera_file = _Asset.capture(record, cameras)
    spec = Cameras.model_validate_json(camera_file.read(record))
    source = record.read_build()
    source_digest = _hash(source.encode("utf-8"))
    state = _structural_state(record)
    checked = (
        state.get("structural_passed")
        and state.get("mpd_path")
        and state.get("snapshot_id")
    )
    with TemporaryDirectory() as scratch:
        root = Path(scratch)
        model = root / "model.ldr"
        if checked:
            model.write_bytes(Path(state["mpd_path"]).read_bytes())
        paths, model_digest = run_in_subprocess(
            _render_worker,
            (record, None if checked else source, model, spec, root / "views"),
            (config or Config()).render_timeout,
        )
        camera_file.read(record)
        if record.source_sha256 != source_digest:
            raise ValueError("build changed during fixed-camera rendering")
        views = {}
        for name, path in paths.items():
            destination = record.out_dir / "views" / f"{name}.png"
            data = path.read_bytes()
            _replace(destination, data)
            asset = _Asset.capture(record, destination)
            if asset.sha256 != _hash(data):
                raise ValueError(f"view {name} changed during rendering")
            views[name] = asset
        render = _Render(
            snapshot_id=state["snapshot_id"] if checked else None,
            source_sha256=source_digest,
            model_sha256=model_digest,
            cameras=camera_file,
            image_pixels=spec.image_pixels,
            views=views,
        )
        _replace(_render_path(record), _json(render.model_dump(mode="json")))
    return render.model_dump(mode="json")


def compare_record(record: Record, reference_dir: Path) -> dict:
    """Save per-view verifier metrics, two-dimensional regions, and overlays."""
    render_asset = _Asset.capture(record, _render_path(record))
    render = _Render.model_validate_json(render_asset.read(record))
    if render.source_sha256 != record.source_sha256:
        raise ValueError("build changed. Render the current version before comparing")
    render.cameras.read(record)
    references, overlays, metrics = {}, {}, {}
    with TemporaryDirectory() as scratch:
        for name, candidate in render.views.items():
            candidate_data = candidate.read(record)
            reference = reference_dir / f"goal-{name}.png"
            references[name] = _Asset.capture(record, reference)
            result, diff = compare_images(
                read_rgba(BytesIO(references[name].read(record))),
                read_rgba(BytesIO(candidate_data)),
            )
            references[name].read(record)
            candidate.read(record)
            metrics[name] = result
            output = record.out_dir / "comparison" / f"{name}.png"
            staging = Path(scratch) / "overlay.png"
            Image.fromarray(diff).save(staging)
            data = staging.read_bytes()
            _replace(output, data)
            overlays[name] = _Asset.capture(record, output)
            if overlays[name].sha256 != _hash(data):
                raise ValueError(f"overlay for {name} changed during comparison")
    render_asset.read(record)
    comparison = _Comparison(
        render=render_asset, references=references, overlays=overlays, metrics=metrics
    )
    output = comparison.model_dump(mode="json")
    _replace(_comparison_path(record), _json(output))
    return output


def _round_path(record: Record, round_id: str) -> Path:
    try:
        if UUID(round_id).hex != round_id:
            raise ValueError
    except ValueError:
        raise ValueError("invalid review round ID") from None
    return record.path / "reviews" / round_id


def _prompt_text(record: Record) -> str:
    return json.loads(record.meta_path.read_bytes()).get("prompt") or ""


def _prepare_round(record: Record, parent_session_id: str | None) -> Path:
    state = _structural_state(record)
    if not (
        state.get("structural_passed")
        and state.get("snapshot_id")
        and state.get("mpd_path")
    ):
        raise ValueError(
            "review requires a current structural snapshot that passed validation"
        )
    descriptions = state["artifacts"]
    if not all(descriptions[name]["valid"] for name in ("conditions", "features")):
        raise ValueError(
            "review requires valid original conditions and a valid feature checklist"
        )
    model = Path(state["mpd_path"])
    snapshot_id = state["snapshot_id"]
    comparison_asset = _Asset.capture(record, _comparison_path(record))
    comparison = _Comparison.model_validate_json(comparison_asset.read(record))
    render = _Render.model_validate_json(comparison.render.read(record))
    model_asset = _Asset.capture(record, model)
    if (
        render.snapshot_id != snapshot_id
        or render.source_sha256 != record.source_sha256
        or render.model_sha256 != model_asset.sha256
    ):
        raise ValueError(
            "render and compare the current structural snapshot before review"
        )

    assets = {
        "conditions.json": _Asset.capture(record, record.path / "conditions.json"),
        "features.json": _Asset.capture(record, record.path / "features.json"),
        "cameras.json": render.cameras,
        "comparison.json": comparison_asset,
        "_render.json": comparison.render,
        "_build.ldr": model_asset,
        "_structure.json": _Asset.capture(record, model.with_name("structure.json")),
    }
    for name in ("conditions", "features"):
        if assets[f"{name}.json"].sha256 != descriptions[name]["sha256"]:
            raise ValueError(f"{name} changed while preparing the review")
    pointer = record.path / "flow/current.json"
    if pointer.is_file():
        assets["_current.json"] = _Asset.capture(record, pointer)
        if assets["_current.json"].sha256 != descriptions["structure"]["sha256"]:
            raise ValueError("structural snapshot changed while preparing the review")
    module_manifest = record.path / "merge-manifest.json"
    if module_manifest.is_file():
        assets["_merge-manifest.json"] = _Asset.capture(record, module_manifest)
    task = record.path / "task.json"
    if task.is_file():
        assets["task.json"] = _Asset.capture(record, task)
    required_images = []
    for view in render.views:
        images = {
            f"goal-{view}.png": comparison.references[view],
            f"candidate-{view}.png": render.views[view],
            f"overlay-{view}.png": comparison.overlays[view],
        }
        assets.update(images)
        required_images.extend(images)
    round_id = uuid4().hex
    destination = _round_path(record, round_id)
    prompt = _prompt_text(record).encode("utf-8") if not task.is_file() else None
    public_files = {
        key: value for key, value in assets.items() if not key.startswith("_")
    }
    files = {
        name: asset.read(record)
        for name, asset in public_files.items()
        if name != "comparison.json"
    }
    if prompt is not None:
        files["prompt.txt"] = prompt
    # Expose the metrics and view names without live paths or builder artifacts.
    files["metrics.json"] = _json(
        {
            "image_pixels": render.image_pixels,
            "views": comparison.metrics,
        }
    )
    public_manifest = {
        "schema_version": 1,
        "round_id": round_id,
        "snapshot_id": snapshot_id,
        "model_sha256": model_asset.sha256,
        "structure_sha256": assets["_structure.json"].sha256,
        "parent_session_id": parent_session_id,
        "image_pixels": render.image_pixels,
        "image_reads": required_images,
        "files": {name: _hash(data) for name, data in files.items()},
    }
    if "_merge-manifest.json" in assets:
        public_manifest["module_manifest_sha256"] = assets[
            "_merge-manifest.json"
        ].sha256
    manifest = _json(public_manifest)
    private = _PrivateInputs(
        manifest_sha256=_hash(manifest),
        snapshot_id=snapshot_id,
        source_sha256=render.source_sha256,
        prompt_sha256=_hash(prompt) if prompt is not None else None,
        parent_session_id=parent_session_id,
        assets=assets,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    private_files = [name for name in assets if name.startswith("_")]
    with TemporaryDirectory(dir=destination.parent) as scratch:
        staging = Path(scratch) / "round"
        packet = staging / "inputs"
        packet.mkdir(parents=True)
        for name, data in (files | {"manifest.json": manifest}).items():
            (packet / name).write_bytes(data)
            (packet / name).chmod(0o444)
        for name in private_files:
            (staging / name).write_bytes(assets[name].read(record))
            (staging / name).chmod(0o444)
        (staging / "bindings.json").write_bytes(_json(private.model_dump(mode="json")))
        (staging / "bindings.json").chmod(0o444)
        staging.rename(destination)
    if not review_inputs_current(record, round_id):
        raise ValueError("review evidence changed while preparing the frozen round")
    return destination


def review_instructions(record: Record, *, parent_session_id: str | None = None) -> str:
    """Freeze the current packet and print the inputs for a fresh external reviewer."""
    round_path = _prepare_round(record, parent_session_id)
    manifest = round_path / "inputs/manifest.json"
    return (
        f"Dispatch a fresh, independent reviewer with no builder history. "
        f"Give it just the frozen packet in {manifest.parent.resolve()} and the instructions below. "
        f"The round ID is {round_path.name}; input_sha256 is {_hash(manifest.read_bytes())}. "
        "Copy the reviewer session and parent session from the dispatch receipt. "
        "Keep the dispatch and image-open evidence. Submit the returned JSON with "
        f"`brickwright external review {record.id} --set REPORT.json`. "
        "If dispatch fails or the reviewer cannot read the images, submit schema_version, "
        "round_id, input_sha256, and failure_reason to archive the failed attempt.\n\n"
        f"{REVIEW_GUIDANCE}\n"
        "Copy all image_reads names from the manifest only after opening those files. "
        "Return this JSON schema with the round ID and digest above.\n"
        f"{json.dumps(Report.model_json_schema(), indent=2)}"
    )


def _read_manifest(round_path: Path) -> tuple[_PrivateInputs, dict]:
    private = _PrivateInputs.model_validate_json(
        (round_path / "bindings.json").read_bytes()
    )
    manifest = (round_path / "inputs/manifest.json").read_bytes()
    if _hash(manifest) != private.manifest_sha256:
        raise ValueError("frozen review manifest changed")
    parsed = json.loads(manifest)
    for name, digest in parsed["files"].items():
        if _hash((round_path / "inputs" / name).read_bytes()) != digest:
            raise ValueError(f"frozen review input changed: {name}")
    for name, asset in private.assets.items():
        if (
            name.startswith("_")
            and _hash((round_path / name).read_bytes()) != asset.sha256
        ):
            raise ValueError(f"frozen structural input changed: {name}")
    return private, parsed


def review_inputs_current(record: Record, round_id: str) -> bool:
    """Check recorded digests without calling flow_status or Record.read_meta."""
    try:
        private, _ = _read_manifest(_round_path(record, round_id))
        if record.source_sha256 != private.source_sha256:
            return False
        if private.prompt_sha256 is None:
            if not (record.path / "task.json").is_file():
                return False
        elif (record.path / "task.json").is_file() or _hash(
            _prompt_text(record).encode("utf-8")
        ) != private.prompt_sha256:
            return False
        if (record.path / "merge-manifest.json").is_file() != (
            "_merge-manifest.json" in private.assets
        ):
            return False
        for asset in private.assets.values():
            asset.read(record)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _check_report(report: Report, packet: Path, manifest: dict) -> list[str]:
    conditions = json.loads((packet / "conditions.json").read_bytes())["checks"]
    features = json.loads((packet / "features.json").read_bytes())["features"]
    checks_by_id = {item["id"]: item for item in conditions}
    features_by_id = {item["id"]: item for item in features}
    for results, ids, label in (
        (report.conditions, checks_by_id, "condition"),
        (report.features, features_by_id, "feature"),
    ):
        seen = [item.id for item in results]
        if set(seen) != set(ids) or len(set(seen)) != len(seen):
            raise ValueError(f"report must include every original {label} exactly once")
    expected_reads = manifest["image_reads"]
    if sorted(report.reviewer.image_reads) != sorted(expected_reads):
        raise ValueError(
            "review receipt must record reading every target, render, and overlay"
        )
    if manifest["parent_session_id"] not in (None, report.reviewer.parent_session_id):
        raise ValueError("review receipt refers to a different parent session")
    width, height = manifest["image_pixels"]
    views = json.loads((packet / "metrics.json").read_bytes())["views"]
    for item in (*report.conditions, *report.features, *report.findings):
        for evidence in item.evidence:
            left, top, right, bottom = evidence.bbox
            if evidence.view not in views or not (
                0 <= left < right <= width and 0 <= top < bottom <= height
            ):
                raise ValueError(
                    "visual evidence must name a view and a valid pixel box"
                )

    remaining = []
    blocked = False
    for condition in report.conditions:
        if condition.status != "satisfied":
            blocked = True
            remaining.append(
                f"{condition.id} {checks_by_id[condition.id]['text']} {condition.repair}"
            )
    for result in report.features:
        feature = features_by_id[result.id]
        if result.status == "simplified" and not (
            feature["category"] == "simplify" and feature.get("simplification")
        ):
            raise ValueError(f"{result.id} has no agreed simplification")
        if result.status in ("partial", "missing"):
            blocked |= feature["importance"] == "required"
            remaining.append(f"{result.id} {feature['name']} {result.repair}")
    for item in report.findings:
        if item.check_id is not None and item.check_id not in checks_by_id:
            raise ValueError(f"unknown original condition {item.check_id} in finding")
        blocked |= item.required
        remaining.append(f"{item.description} {item.repair}")
    if blocked != (report.verdict == "needs_changes"):
        raise ValueError(
            "review verdict must follow the original conditions and required features"
        )
    return remaining


def _submit(record: Record, raw: bytes) -> dict:
    data = TypeAdapter(dict[str, Any]).validate_json(raw)
    report = (FailedReport if "failure_reason" in data else Report).model_validate(data)
    round_path = _round_path(record, report.round_id)
    private, manifest = _read_manifest(round_path)
    if manifest["round_id"] != report.round_id:
        raise ValueError("review manifest belongs to another round")
    if report.input_sha256 != private.manifest_sha256:
        raise ValueError("review report does not match the original input digest")
    report_data = report.model_dump(mode="json")
    destination = round_path / "review.json"
    if destination.is_file():
        existing = json.loads(destination.read_bytes())
        if existing["report"] != report_data:
            raise ValueError("this review round already has a different report")
        return existing

    remaining: list[str] = []
    if isinstance(report, FailedReport):
        status = "failed"
    else:
        remaining = _check_report(report, round_path / "inputs", manifest)
        state = _structural_state(record)
        status = (
            "accepted"
            if state.get("structural_passed")
            and state.get("snapshot_id") == private.snapshot_id
            and review_inputs_current(record, report.round_id)
            else "stale"
        )
    result = {
        "schema_version": 1,
        "round_id": report.round_id,
        "input_sha256": private.manifest_sha256,
        "snapshot_id": private.snapshot_id,
        "submitted_at": datetime.now(UTC).isoformat(),
        "status": status,
        "passed": status == "accepted"
        and isinstance(report, Report)
        and report.verdict == "passed",
        "remaining_items": remaining,
        "report": report_data,
    }
    # One immutable receipt per round, including with concurrent/repeated submissions.
    stage = round_path / f".{uuid4().hex}.tmp"
    try:
        stage.write_bytes(_json(result))
        stage.chmod(0o444)
        try:
            os.link(stage, destination)
        except FileExistsError:
            existing = json.loads(destination.read_bytes())
            if existing["report"] != report_data:
                raise ValueError(
                    "this review round already has a different report"
                ) from None
            return existing
    finally:
        stage.unlink(missing_ok=True)
    if status == "accepted":
        _replace(record.path / "review.json", _json(result))
    return result


def submit_review(record: Record, path: Path) -> dict:
    """Store a validated independent review or archive a failed/stale submission."""
    return _submit(record, path.read_bytes())


def review_status(record: Record, snapshot_id: str) -> dict:
    """Count accepted rounds and return the latest verdict for current inputs."""
    current = []
    rounds = 0
    for path in (record.path / "reviews").glob("*/review.json"):
        try:
            receipt = json.loads(path.read_bytes())
            if receipt["status"] != "accepted":
                continue
            rounds += 1
            if (
                receipt["snapshot_id"] == snapshot_id
                and receipt["round_id"] == path.parent.name
                and receipt["input_sha256"]
                == _hash((path.parent / "inputs/manifest.json").read_bytes())
                and review_inputs_current(record, path.parent.name)
            ):
                current.append(receipt)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    latest = max(
        current, key=lambda row: (row["submitted_at"], row["round_id"]), default=None
    )
    return {
        "passed": latest["passed"] if latest else False,
        "rounds": rounds,
        "remaining_items": latest["remaining_items"] if latest else [],
        "current": latest is not None,
    }


def run_independent_review(
    record: Record,
    config: Config,
    on_response: Callable[[Any], None] | None = None,
    *,
    complete: Callable[..., Any] | None = None,
) -> dict:
    """Call a fresh image-capable critic with only the frozen review evidence."""
    from .driver.completion import litellm_complete
    from .driver.messages import image_content_block, request_kwargs

    model, effort = config.agent_model_and_effort("critique")
    if not model:
        raise ValueError(
            "configure a critique model before requesting an independent review"
        )
    parent = f"brickwright-builder:{record.id}"
    round_path = _prepare_round(record, parent)
    private, manifest = _read_manifest(round_path)
    packet = round_path / "inputs"
    content = []
    for name in manifest["files"]:
        content.append({"type": "text", "text": name})
        path = packet / name
        content.append(
            image_content_block(path)
            if name in manifest["image_reads"]
            else {"type": "text", "text": path.read_text(encoding="utf-8")}
        )
    messages = [
        {
            "role": "system",
            "content": REVIEW_GUIDANCE
            + "\nReturn only JSON with this schema:\n"
            + json.dumps(Judgment.model_json_schema()),
        },
        {"role": "user", "content": content},
    ]
    round_info = {
        "schema_version": 1,
        "round_id": round_path.name,
        "input_sha256": private.manifest_sha256,
    }
    complete = complete or litellm_complete()
    try:
        response = complete(messages=messages, **request_kwargs(config, "critique"))
    except Exception as exc:
        _submit(record, _json(round_info | {"failure_reason": type(exc).__name__}))
        raise
    if on_response is not None:
        on_response(response)
    try:
        judgment = Judgment.model_validate_json(
            response.choices[0].message.content or ""
        )
        reviewer = Reviewer(
            session_id=response.id,
            parent_session_id=parent,
            dispatch="independent_model",
            model=model,
            effort=effort.value,
            image_reads=manifest["image_reads"],
        )
        return _submit(
            record,
            _json(
                round_info
                | judgment.model_dump()
                | {
                    "reviewer": reviewer.model_dump(),
                }
            ),
        )
    except (AttributeError, IndexError, ValidationError, ValueError) as exc:
        return _submit(record, _json(round_info | {"failure_reason": str(exc)}))
