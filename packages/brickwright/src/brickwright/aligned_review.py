"""Fixed-camera rendering, comparisons, and host-led visual review."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from importlib import import_module
from io import BytesIO
from pathlib import Path
from shlex import quote
from tempfile import TemporaryDirectory
from typing import Any, Literal
from uuid import UUID, uuid4

from PIL import Image, ImageOps
from pydantic import BaseModel, TypeAdapter

from ._aligned_compare import compare_images, read_rgba
from ._aligned_render import Cameras, MatchedAngle, render_matched_model, render_model
from ._review_reports import (
    FIXED_GUIDANCE,
    MATCHED_GUIDANCE,
    REVIEW_GUIDANCE,
    FailedReport,
    Report,
)
from .config import Config
from .record import Record
from .tools.build_execution import load_build_module, run_in_subprocess
from .tools.testing import CheckResult
from .tools.validation import ValidationReport


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
    schema_version: Literal[1] = 1
    snapshot_id: str | None
    source_sha256: str
    model_sha256: str
    cameras: _Asset
    image_pixels: tuple[int, int]
    views: dict[str, _Asset]
    alignment: Literal["fixed_cameras", "matched_viewpoint"] = "fixed_cameras"


class _Comparison(BaseModel):
    schema_version: Literal[1] = 1
    render: _Asset
    references: dict[str, _Asset]
    overlays: dict[str, _Asset]
    metrics: dict[str, dict[str, Any]]
    image_pixels: tuple[int, int] | None = None


class _PrivateInputs(BaseModel):
    schema_version: Literal[1] = 1
    manifest_sha256: str
    snapshot_id: str
    source_sha256: str
    prompt_sha256: str | None
    parent_session_id: str | None
    prompt_image: str | None = None
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
    if render.alignment != "fixed_cameras":
        raise ValueError("pixel comparison requires fixed-camera renders")
    render.cameras.read(record)
    missing = [
        f"goal-{name}.png"
        for name in render.views
        if not (reference_dir / f"goal-{name}.png").is_file()
    ]
    if missing:
        raise ValueError(
            f"Missing reference images in {reference_dir}: {', '.join(missing)}. "
            "Name each target image goal-<camera>.png for the cameras "
            f"{', '.join(render.views)}."
        )
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


def _require_structure(record: Record) -> dict:
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
    return state


def _prompt_image_name(record: Record) -> str | None:
    return json.loads(record.meta_path.read_bytes()).get("prompt_image")


def _prepare_matched_view(
    record: Record, angles: tuple[float, float], config: Config | None
) -> None:
    state = _require_structure(record)
    image_name = _prompt_image_name(record)
    if not image_name:
        raise ValueError("matched viewpoint review requires a prompt image")
    prompt_image = _Asset.capture(record, record.path / image_name)
    with Image.open(BytesIO(prompt_image.read(record))) as image:
        reference_size = ImageOps.exif_transpose(image).size

    camera = MatchedAngle(azimuth=angles[0], elevation=angles[1])
    source_digest = record.source_sha256
    model = Path(state["mpd_path"]).read_bytes()
    with TemporaryDirectory() as directory:
        scratch = Path(directory)
        mpd, rendered = scratch / "model.ldr", scratch / "matched.png"
        mpd.write_bytes(model)
        run_in_subprocess(
            render_matched_model,
            (mpd, camera, rendered),
            (config or Config()).render_timeout,
        )
        with Image.open(rendered) as image:
            candidate_size = image.size
        if source_digest != record.source_sha256:
            raise ValueError("build changed during matched-viewpoint rendering")
        camera_file = record.out_dir / "matched-camera.json"
        candidate = record.out_dir / "views/matched.png"
        camera_spec = camera.model_dump(mode="json") | {"method": "matched_viewpoint"}
        _replace(camera_file, _json(camera_spec))
        color_data = rendered.read_bytes()
        _replace(candidate, color_data)
    view = _Asset.capture(record, candidate)
    if view.sha256 != _hash(color_data):
        raise ValueError("matched view changed during rendering")
    render = _Render(
        snapshot_id=state["snapshot_id"],
        source_sha256=source_digest,
        model_sha256=_hash(model),
        cameras=_Asset.capture(record, camera_file),
        image_pixels=candidate_size,
        views={"matched": view},
        alignment="matched_viewpoint",
    )
    _replace(_render_path(record), _json(render.model_dump(mode="json")))
    comparison = _Comparison(
        render=_Asset.capture(record, _render_path(record)),
        references={"matched": prompt_image},
        overlays={},
        metrics={"matched": {"pixel_scores": None, "regions": []}},
        image_pixels=reference_size,
    )
    _replace(_comparison_path(record), _json(comparison.model_dump(mode="json")))


def _validation_asset(
    record: Record, snapshot_id: str, source_sha256: str, pointer: _Asset
) -> _Asset | None:
    """Find the archived validation report bound to the dispatched structure."""
    try:
        raw = record.report_path.read_bytes()
        report = ValidationReport.model_validate_json(raw)
        flow = report.flow or {}
        if not (
            report.ok
            and report.report_path
            and report.source_sha256 == source_sha256
            and flow.get("structural_passed") is True
            and flow.get("snapshot_id") == snapshot_id
            and flow["artifacts"]["structure"]["sha256"] == pointer.sha256
        ):
            return None
        path = record.path / report.report_path
        if (
            path.parent.parent != record.snapshots_dir
            or not path.parent.name.isdecimal()
            or path.name != record.report_path.name
        ):
            return None
        asset = _Asset.capture(record, path)
        return asset if asset.sha256 == _hash(raw) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _prepare_round(record: Record, parent_session_id: str | None) -> Path:
    state = _require_structure(record)
    descriptions = state["artifacts"]
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
        pointer_asset = _Asset.capture(record, pointer)
        if (
            pointer_asset.sha256 != descriptions["structure"]["sha256"]
            or json.loads(pointer_asset.read(record))["structure_sha256"]
            != assets["_structure.json"].sha256
        ):
            raise ValueError("structural snapshot changed while preparing the review")
        assets["_current.json"] = pointer_asset
        validation = _validation_asset(
            record, snapshot_id, render.source_sha256, pointer_asset
        )
        if validation is not None:
            assets["_validation.json"] = validation
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
        }
        if view in comparison.overlays:
            images[f"overlay-{view}.png"] = comparison.overlays[view]
        assets.update(images)
        required_images.extend(images)
    round_id = uuid4().hex
    destination = _round_path(record, round_id)
    prompt = _prompt_text(record).encode("utf-8") if not task.is_file() else None
    prompt_image = None
    if render.alignment == "matched_viewpoint":
        prompt_image = _prompt_image_name(record)
        if (
            not prompt_image
            or _Asset.capture(record, record.path / prompt_image)
            != comparison.references["matched"]
        ):
            raise ValueError(
                "prompt image changed; match the current reference viewpoint"
            )
    public_files = {
        key: value for key, value in assets.items() if not key.startswith("_")
    }
    files = {
        name: asset.read(record)
        for name, asset in public_files.items()
        if name != "comparison.json"
    }
    if prompt_image is not None:
        target = "goal-matched.png"
        with Image.open(BytesIO(files[target])) as image:
            png = BytesIO()
            ImageOps.exif_transpose(image).convert("RGBA").save(png, format="PNG")
            files[target] = png.getvalue()
    if prompt is not None:
        files["prompt.txt"] = prompt
    # Expose the metrics and view names without live paths or builder artifacts.
    image_pixels = comparison.image_pixels or render.image_pixels
    files["metrics.json"] = _json(
        {
            "image_pixels": image_pixels,
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
        "image_pixels": image_pixels,
        "alignment": render.alignment,
        "image_reads": required_images,
        "files": {name: _hash(data) for name, data in files.items()},
    }
    if "_merge-manifest.json" in assets:
        public_manifest["module_manifest_sha256"] = assets[
            "_merge-manifest.json"
        ].sha256
    if "_validation.json" in assets:
        public_manifest["validation_sha256"] = assets["_validation.json"].sha256
    manifest = _json(public_manifest)
    private = _PrivateInputs(
        manifest_sha256=_hash(manifest),
        snapshot_id=snapshot_id,
        source_sha256=render.source_sha256,
        prompt_sha256=_hash(prompt) if prompt is not None else None,
        parent_session_id=parent_session_id,
        prompt_image=prompt_image,
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


def review_instructions(
    record: Record,
    *,
    parent_session_id: str | None = None,
    matched_angle: tuple[float, float] | None = None,
    config: Config | None = None,
) -> str:
    """Freeze the current packet and describe how to submit a review."""
    if matched_angle is not None:
        _prepare_matched_view(record, matched_angle, config)
    if not _comparison_path(record).is_file():
        if message := _compare_first(record):
            raise ValueError(message)
        return _match_viewpoint_instructions(record)
    round_path = _prepare_round(record, parent_session_id)
    manifest = round_path / "inputs/manifest.json"
    manifest_bytes = manifest.read_bytes()
    guidance = _review_guidance(json.loads(manifest_bytes))
    records_dir = quote(str(record.path.parent.resolve()))
    record_id = quote(record.id)
    return (
        "Open the frozen images yourself (dispatch=self, both session IDs are the builder ID), "
        "or dispatch a fresh, independent reviewer with no builder history "
        "(dispatch=subagent, use the actual child and parent IDs). "
        f"Review the frozen packet in {manifest.parent.resolve()}. "
        f"The round ID is {round_path.name}; input_sha256 is {_hash(manifest_bytes)}. "
        "Submit the review JSON with "
        f"`brickwright external review {record_id} --set REPORT.json --records-dir {records_dir}`. "
        "If dispatch fails or the reviewer cannot read the images, submit schema_version, "
        "round_id, input_sha256, and failure_reason to archive the failed attempt.\n\n"
        f"{guidance}\n"
        "Copy all image_reads names from the manifest only after opening those files. "
        "Return this JSON schema with the round ID and digest above.\n"
        f"{json.dumps(Report.model_json_schema(), indent=2)}"
    )


def _compare_first(record: Record) -> str | None:
    """Explain the missing comparison after a fixed-camera render."""
    try:
        data = _render_path(record).read_bytes()
    except FileNotFoundError:
        return None
    render = _Render.model_validate_json(data)
    if render.alignment != "fixed_cameras":
        return None
    names = ", ".join(f"goal-{name}.png" for name in render.views)
    records_dir = quote(str(record.path.parent.resolve()))
    return (
        "Fixed-camera views are rendered but not compared with the targets. Run "
        f"`brickwright external compare {quote(record.id)} --reference-dir DIR "
        f"--records-dir {records_dir}`, where DIR contains {names}, then request "
        "the review instructions again."
    )


def _review_guidance(manifest: dict) -> str:
    matched = manifest.get("alignment") == "matched_viewpoint"
    return (MATCHED_GUIDANCE if matched else FIXED_GUIDANCE) + "\n\n" + REVIEW_GUIDANCE


def _match_viewpoint_instructions(record: Record) -> str:
    from .external_critique import external_viewpoint_prompt

    _require_structure(record)
    image_name = _prompt_image_name(record)
    if not image_name:
        raise ValueError(
            "render and compare fixed camera views, or attach a prompt image"
        )
    match = external_viewpoint_prompt(record)
    records_dir = quote(str(record.path.parent.resolve()))
    record_id = quote(record.id)
    return (
        "Match the viewpoint yourself or give these instructions to a viewpoint-matching agent. "
        "The selected angles will be used to render the checked snapshot for visual review.\n"
        f"Reference: {(record.path / image_name).resolve()}\n"
        f"Build source for viewpoint matcher: {record.build_path.resolve()}\n\n"
        f"{match}\n\n"
        "Use the returned angles to prepare the review packet:\n"
        f"brickwright external review-instructions {record_id} --azimuth <A> --elevation <E> "
        f"--records-dir {records_dir}"
    )


def _read_manifest(round_path: Path) -> tuple[_PrivateInputs, dict]:
    private = _PrivateInputs.model_validate_json(
        (round_path / "bindings.json").read_bytes()
    )
    manifest = (round_path / "inputs/manifest.json").read_bytes()
    if _hash(manifest) != private.manifest_sha256:
        raise ValueError("frozen review manifest changed")
    parsed = json.loads(manifest)
    validation = private.assets.get("_validation.json")
    if (
        parsed["round_id"] != round_path.name
        or parsed["snapshot_id"] != private.snapshot_id
        or parsed["parent_session_id"] != private.parent_session_id
        or parsed["model_sha256"] != private.assets["_build.ldr"].sha256
        or parsed["structure_sha256"] != private.assets["_structure.json"].sha256
        or parsed.get("validation_sha256")
        != (validation.sha256 if validation else None)
    ):
        raise ValueError("frozen review manifest bindings changed")
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


def _live_inputs_match(record: Record, private: _PrivateInputs) -> bool:
    if record.source_sha256 != private.source_sha256:
        return False
    task_exists = (record.path / "task.json").is_file()
    if private.prompt_sha256 is None:
        if not task_exists:
            return False
    elif (
        task_exists
        or _hash(_prompt_text(record).encode("utf-8")) != private.prompt_sha256
    ):
        return False
    if (record.path / "merge-manifest.json").is_file() != (
        "_merge-manifest.json" in private.assets
    ):
        return False
    if (
        private.prompt_image is not None
        and _prompt_image_name(record) != private.prompt_image
    ):
        return False
    for asset in private.assets.values():
        asset.read(record)
    return True


def review_inputs_current(record: Record, round_id: str) -> bool:
    """Check recorded digests without calling flow_status or Record.read_meta."""
    try:
        private, _ = _read_manifest(_round_path(record, round_id))
        return _live_inputs_match(record, private)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _check_report(
    report: Report, packet: Path, manifest: dict, structural_checks: list[CheckResult]
) -> tuple[list[str], dict[str, list[str]], bool]:
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
            "review receipt must record reading every target, render, and provided overlay"
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
    structural_evidence = {}
    structural_unresolved = False
    blocked = False
    for condition in report.conditions:
        if condition.status == "structural_evidence_required":
            prefix = f"{condition.id}_"
            matching = [
                check
                for check in structural_checks
                if check.name == condition.id or check.name.startswith(prefix)
            ]
            if matching and all(check.passed for check in matching):
                structural_evidence[condition.id] = [check.name for check in matching]
            else:
                structural_unresolved = True
                remaining.append(
                    f"{condition.id} {checks_by_id[condition.id]['text']} "
                    f"No passing frozen check named {condition.id} or {prefix}* "
                    f"was found with every match passing. {condition.repair}"
                )
            continue
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
    return remaining, structural_evidence, structural_unresolved


def _publish_once(path: Path, data: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.parent / f".{uuid4().hex}.tmp"
    try:
        stage.write_bytes(data)
        stage.chmod(0o444)
        try:
            os.link(stage, path)
        except FileExistsError:
            return False
        return True
    finally:
        stage.unlink(missing_ok=True)


def _claim_session(record: Record, report: Report) -> None:
    session = _hash(report.reviewer.session_id.encode("utf-8"))
    path = record.path / "reviews" / "sessions" / session
    owner = report.round_id.encode("ascii")
    if not _publish_once(path, owner) and path.read_bytes() != owner:
        raise ValueError("this independent session already reviewed another round")


def _submit(record: Record, raw: bytes) -> dict:
    data = TypeAdapter(dict[str, Any]).validate_json(raw)
    report = (FailedReport if "failure_reason" in data else Report).model_validate(data)
    round_path = _round_path(record, report.round_id)
    private, manifest = _read_manifest(round_path)
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
    structural_evidence: dict[str, list[str]] = {}
    structural_unresolved = False
    if isinstance(report, FailedReport):
        status = "failed"
    else:
        structural_checks = []
        if "_validation.json" in private.assets:
            structural_checks = ValidationReport.model_validate_json(
                (round_path / "_validation.json").read_bytes()
            ).checks
        remaining, structural_evidence, structural_unresolved = _check_report(
            report, round_path / "inputs", manifest, structural_checks
        )
        if report.reviewer.dispatch != "self":
            _claim_session(record, report)
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
        and report.verdict == "passed"
        and not structural_unresolved,
        "remaining_items": remaining,
        "structural_evidence": structural_evidence,
        "report": report_data,
    }
    # One immutable receipt per round, including with concurrent/repeated submissions.
    if not _publish_once(destination, _json(result)):
        existing = json.loads(destination.read_bytes())
        if existing["report"] != report_data:
            raise ValueError("this review round already has a different report")
        return existing
    if status == "accepted":
        _replace(record.path / "review.json", _json(result))
    return result


def submit_review(record: Record, path: Path) -> dict:
    """Store a validated visual review or archive a failed/stale submission."""
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
            inputs, _ = _read_manifest(path.parent)
            if (
                receipt["round_id"] != path.parent.name
                or receipt["snapshot_id"] != inputs.snapshot_id
                or receipt["input_sha256"] != inputs.manifest_sha256
            ):
                continue
            rounds += 1
            if inputs.snapshot_id == snapshot_id and _live_inputs_match(record, inputs):
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
        "dispatch": latest["report"]["reviewer"]["dispatch"] if latest else None,
    }
