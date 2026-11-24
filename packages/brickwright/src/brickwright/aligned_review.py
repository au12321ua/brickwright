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
from pydantic import BaseModel, TypeAdapter, ValidationError

from ._aligned_compare import compare_images, read_rgba
from ._aligned_render import Cameras, MatchedAngle, render_matched_model, render_model
from ._review_reports import (
    FIXED_GUIDANCE,
    MATCHED_GUIDANCE,
    REVIEW_GUIDANCE,
    FailedReport,
    Finding,
    Report,
)
from .actions import (
    FILE,
    RECORD,
    FlowError,
    edit,
    provide,
    run,
    validation_errors,
    validation_message,
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
            raise FlowError(
                f"A review input changed after it was recorded: {self.path}.",
                code="flow.changed",
                evidence={"path": self.path},
            )
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


CAMERAS = "CAMERAS_JSON"
REFERENCES = "REFERENCE_DIR"


def render_action(cameras: Path | None = None):
    """Render the checked snapshot at the task cameras."""
    if cameras is not None and cameras.is_file():
        return run(
            "Render the checked snapshot at the task cameras.",
            "external",
            "render",
            RECORD,
            "--cameras",
            str(cameras.absolute()),
        )
    return provide(
        "Provide the task's camera file and render the checked snapshot at those cameras.",
        run(
            "Render the checked snapshot at the task cameras.",
            "external",
            "render",
            RECORD,
            "--cameras",
            CAMERAS,
        ),
        placeholders=(CAMERAS,),
    )


def compare_action(views: list[str], reference_dir: Path | None = None):
    """Compare the rendered views with goal-<camera>.png targets."""
    names = ", ".join(f"goal-{view}.png" for view in views)
    if reference_dir is not None and all(
        (reference_dir / f"goal-{view}.png").is_file() for view in views
    ):
        return run(
            f"Compare the rendered views with {names}.",
            "external",
            "compare",
            RECORD,
            "--reference-dir",
            str(reference_dir.absolute()),
        )
    return provide(
        f"Provide a directory containing {names}, one target per camera, and compare.",
        run(
            f"Compare the rendered views with {names}.",
            "external",
            "compare",
            RECORD,
            "--reference-dir",
            REFERENCES,
        ),
        placeholders=(REFERENCES,),
        path=None if reference_dir is None else reference_dir.absolute(),
    )


def _new_round_action():
    return run(
        "Prepare a new review round for the current snapshot.",
        "external",
        "review-instructions",
        RECORD,
    )


def _report_fix(message: str, *, fields: tuple[str, ...], **evidence) -> FlowError:
    return FlowError(
        message,
        code="review.report_invalid",
        evidence=evidence,
        action=edit(
            "Correct the named fields in the same report and submit it to the same round.",
            FILE,
            run(
                "Submit the corrected report.",
                "external",
                "review",
                RECORD,
                "--set",
                FILE,
            ),
            fields=fields,
        ),
        extra={"round_open": True},
    )


def _cameras_path(record: Record, render: _Render) -> Path | None:
    path = record.path / render.cameras.path
    return path if render.alignment == "fixed_cameras" and path.is_file() else None


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
    if not cameras.is_file():
        raise FlowError(
            f"The camera file {cameras} does not exist.",
            code="flow.invalid_input",
            evidence={"path": str(cameras)},
            action=render_action(),
        )
    camera_file = _Asset.capture(record, cameras)
    try:
        spec = Cameras.model_validate_json(camera_file.read(record))
    except ValidationError as exc:
        errors = validation_errors(exc)
        raise FlowError(
            validation_message(f"The camera file {cameras}", errors),
            code="flow.invalid_input",
            evidence={"path": str(cameras), "errors": errors},
            action=render_action(),
        ) from None
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
        try:
            paths, model_digest = run_in_subprocess(
                _render_worker,
                (record, None if checked else source, model, spec, root / "views"),
                (config or Config()).render_timeout,
            )
        except Exception as exc:
            raise _render_failure(exc, cameras) from exc
        camera_file.read(record)
        if record.source_sha256 != source_digest:
            raise FlowError(
                "build.py changed during fixed-camera rendering.",
                code="flow.changed",
                action=render_action(cameras),
            )
        views = {}
        for name, path in paths.items():
            destination = record.out_dir / "views" / f"{name}.png"
            data = path.read_bytes()
            _replace(destination, data)
            asset = _Asset.capture(record, destination)
            if asset.sha256 != _hash(data):
                raise FlowError(
                    f"View {name} changed during rendering.",
                    code="flow.changed",
                    action=render_action(cameras),
                )
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


def _render_failure(exc: Exception, cameras: Path) -> Exception:
    """Name a missing LDraw library; leave every other failure unchanged."""
    kind = getattr(exc, "exception_type", type(exc).__name__)
    if kind != "LDrawLibraryNotFound":
        return exc
    return FlowError(
        "Rendering needs the LDraw parts library, which was not found.",
        code="render.ldraw_missing",
        evidence={"cause": str(exc)},
        action=provide(
            "Set LDRAW_LIBRARY_PATH to the root of the LDraw parts library, then render again.",
            run(
                "Render the checked snapshot at the task cameras.",
                "external",
                "render",
                RECORD,
                "--cameras",
                str(cameras.absolute()),
            ),
        ),
    )


def compare_record(record: Record, reference_dir: Path) -> dict:
    """Save per-view verifier metrics, two-dimensional regions, and overlays."""
    if not _render_path(record).is_file():
        raise FlowError(
            "Render the fixed camera views before comparing them.",
            code="review.render_missing",
            action=render_action(),
        )
    render_asset = _Asset.capture(record, _render_path(record))
    render = _Render.model_validate_json(render_asset.read(record))
    if render.alignment != "fixed_cameras":
        raise FlowError(
            "Pixel comparison needs fixed-camera renders; the current render is a matched viewpoint.",
            code="review.render_missing",
            action=render_action(),
        )
    if render.source_sha256 != record.source_sha256:
        raise FlowError(
            "build.py changed after rendering. Render the current version before comparing.",
            code="review.render_stale",
            action=render_action(_cameras_path(record, render)),
        )
    render.cameras.read(record)
    missing = [
        f"goal-{name}.png"
        for name in render.views
        if not (reference_dir / f"goal-{name}.png").is_file()
    ]
    if missing:
        raise FlowError(
            f"Missing reference images in {reference_dir}: {', '.join(missing)}. "
            "Name each target image goal-<camera>.png for the cameras "
            f"{', '.join(render.views)}.",
            code="review.references_missing",
            evidence={"directory": str(reference_dir), "missing": missing},
            action=compare_action(list(render.views)),
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
                raise FlowError(
                    f"The overlay for {name} changed during comparison.",
                    code="flow.changed",
                    action=compare_action(list(render.views), reference_dir),
                )
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
        raise _report_fix(
            "round_id is not a review round ID.",
            fields=("round_id",),
            round_id=round_id,
        ) from None
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
        raise FlowError(
            "Review needs a current structural snapshot that passed validation.",
            code="flow.structure",
        )
    descriptions = state["artifacts"]
    if not all(descriptions[name]["valid"] for name in ("conditions", "features")):
        raise FlowError(
            "Review needs valid original conditions and a valid feature checklist.",
            code="flow.features",
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
        raise FlowError(
            "A matched-viewpoint review needs a prompt image on the record.",
            code="flow.invalid_input",
        )
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
            raise FlowError(
                "build.py changed during matched-viewpoint rendering.",
                code="flow.changed",
            )
        camera_file = record.out_dir / "matched-camera.json"
        candidate = record.out_dir / "views/matched.png"
        camera_spec = camera.model_dump(mode="json") | {"method": "matched_viewpoint"}
        _replace(camera_file, _json(camera_spec))
        color_data = rendered.read_bytes()
        _replace(candidate, color_data)
    view = _Asset.capture(record, candidate)
    if view.sha256 != _hash(color_data):
        raise FlowError(
            "The matched view changed during rendering.", code="flow.changed"
        )
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
        raise FlowError(
            "The render is older than the current structural snapshot. "
            "Render and compare the current snapshot before review.",
            code="review.render_stale",
            action=render_action(_cameras_path(record, render)),
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
            raise FlowError(
                f"{name}.json changed while preparing the review.",
                code="flow.changed",
                action=_new_round_action(),
            )
    pointer = record.path / "flow/current.json"
    if pointer.is_file():
        pointer_asset = _Asset.capture(record, pointer)
        if (
            pointer_asset.sha256 != descriptions["structure"]["sha256"]
            or json.loads(pointer_asset.read(record))["structure_sha256"]
            != assets["_structure.json"].sha256
        ):
            raise FlowError(
                "The structural snapshot changed while preparing the review.",
                code="flow.changed",
                action=_new_round_action(),
            )
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
            raise FlowError(
                "The prompt image changed. Match the current reference viewpoint again.",
                code="review.render_stale",
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
        raise FlowError(
            "Review evidence changed while preparing the frozen round.",
            code="flow.changed",
            action=_new_round_action(),
        )
    return destination


def review_instructions(
    record: Record,
    *,
    parent_session_id: str | None = None,
    matched_angle: tuple[float, float] | None = None,
    config: Config | None = None,
) -> str:
    """Freeze the current packet and return the reviewer instructions."""
    prepared = prepare_review(
        record,
        parent_session_id=parent_session_id,
        matched_angle=matched_angle,
        config=config,
    )
    return prepared["instructions"]


def prepare_review(
    record: Record,
    *,
    parent_session_id: str | None = None,
    matched_angle: tuple[float, float] | None = None,
    config: Config | None = None,
) -> dict[str, Any]:
    """Freeze a review round, write its report template and instructions."""
    if matched_angle is not None:
        _prepare_matched_view(record, matched_angle, config)
    if not _comparison_path(record).is_file():
        if error := _compare_first(record):
            raise error
        return _match_viewpoint_instructions(record)
    round_path = _prepare_round(record, parent_session_id)
    packet = round_path / "inputs"
    manifest_bytes = (packet / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    digest = _hash(manifest_bytes)
    report_path = round_path / "report.json"
    report_path.write_bytes(_json(_report_template(packet, manifest, digest)))
    instructions = (
        f"Review the frozen packet in {packet.resolve()}. Open the frozen images "
        "yourself for a self-review (dispatch=self, keep both prefilled session IDs), "
        "or give this file and the packet to a fresh, independent reviewer with no "
        "builder history (dispatch=subagent, set reviewer.session_id to the reviewer's "
        f"own session ID). Fill in {report_path.resolve()}: it already holds the round "
        f"ID {round_path.name}, the input digest {digest}, and every condition and "
        "feature to judge. If the images cannot be read, replace the report with "
        "schema_version, round_id, input_sha256 and failure_reason to archive the "
        "failed attempt. The builder submits the report.\n\n"
        f"{_review_guidance(manifest)}\n"
        "Copy all image_reads names from the manifest only after opening those files. "
        "The report follows this JSON schema.\n"
        f"{json.dumps(Report.model_json_schema(), indent=2)}"
    )
    instructions_path = round_path / "instructions.md"
    instructions_path.write_text(instructions + "\n", encoding="utf-8")
    return {
        "mode": "review",
        "round_id": round_path.name,
        "input_sha256": digest,
        "packet_dir": str(packet.resolve()),
        "report_path": str(report_path.resolve()),
        "instructions_path": str(instructions_path.resolve()),
        "instructions": instructions,
        "next": edit(
            "Open instructions.md and the packet images, judge every condition and "
            "feature, and fill in the report template.",
            report_path.resolve(),
            run(
                "Submit the report.",
                "external",
                "review",
                RECORD,
                "--set",
                str(report_path.resolve()),
            ),
            fields=("conditions", "features", "findings", "verdict", "reviewer"),
        ),
    }


def _report_template(packet: Path, manifest: dict, digest: str) -> dict[str, Any]:
    conditions = json.loads((packet / "conditions.json").read_bytes())["checks"]
    features = json.loads((packet / "features.json").read_bytes())["features"]
    session = manifest["parent_session_id"] or "builder"
    return {
        "schema_version": 1,
        "round_id": manifest["round_id"],
        "input_sha256": digest,
        "reviewer": {
            "session_id": session,
            "parent_session_id": session,
            "model": "",
            "effort": "",
            "dispatch": "self",
            "image_reads": [],
        },
        "conditions": [
            {"id": item["id"], "status": "", "explanation": "", "evidence": []}
            for item in conditions
        ],
        "features": [
            {"id": item["id"], "status": "", "evidence": []} for item in features
        ],
        "findings": [],
        "verdict": "",
    }


def _compare_first(record: Record) -> FlowError | None:
    """Explain the missing comparison after a fixed-camera render."""
    try:
        data = _render_path(record).read_bytes()
    except FileNotFoundError:
        return None
    render = _Render.model_validate_json(data)
    if render.alignment != "fixed_cameras":
        return None
    names = ", ".join(f"goal-{name}.png" for name in render.views)
    return FlowError(
        f"Fixed-camera views are rendered but not compared with the targets ({names}).",
        code="review.comparison_missing",
        action=compare_action(list(render.views)),
    )


def _review_guidance(manifest: dict) -> str:
    matched = manifest.get("alignment") == "matched_viewpoint"
    return (MATCHED_GUIDANCE if matched else FIXED_GUIDANCE) + "\n\n" + REVIEW_GUIDANCE


def _match_viewpoint_instructions(record: Record) -> dict[str, Any]:
    from .external_critique import external_viewpoint_prompt

    _require_structure(record)
    image_name = _prompt_image_name(record)
    if not image_name:
        raise FlowError(
            "Review needs fixed-camera renders compared with targets, or a prompt image "
            "for a matched viewpoint.",
            code="review.render_missing",
            action=render_action(),
        )
    match = external_viewpoint_prompt(record)
    records_dir = quote(str(record.path.parent.resolve()))
    record_id = quote(record.id)
    instructions = (
        "Match the viewpoint yourself or give these instructions to a viewpoint-matching agent. "
        "The selected angles will be used to render the checked snapshot for visual review.\n"
        f"Reference: {(record.path / image_name).resolve()}\n"
        f"Build source for viewpoint matcher: {record.build_path.resolve()}\n\n"
        f"{match}\n\n"
        "Use the returned angles to prepare the review packet:\n"
        f"brickwright external review-instructions {record_id} --azimuth <A> --elevation <E> "
        f"--records-dir {records_dir}"
    )
    return {
        "mode": "match_viewpoint",
        "instructions": instructions,
        "next": provide(
            "Match the reference viewpoint with render-angle as the instructions describe, "
            "then prepare the review at the matched angles.",
            run(
                "Prepare the review at the matched angles.",
                "external",
                "review-instructions",
                RECORD,
                "--azimuth",
                "AZIMUTH",
                "--elevation",
                "ELEVATION",
            ),
            placeholders=("AZIMUTH", "ELEVATION"),
        ),
    }


def _read_manifest(round_path: Path) -> tuple[_PrivateInputs, dict]:
    private = _PrivateInputs.model_validate_json(
        (round_path / "bindings.json").read_bytes()
    )
    manifest = (round_path / "inputs/manifest.json").read_bytes()
    if _hash(manifest) != private.manifest_sha256:
        raise _packet_changed("The frozen review manifest changed.")
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
        raise _packet_changed("The frozen review manifest bindings changed.")
    for name, digest in parsed["files"].items():
        if _hash((round_path / "inputs" / name).read_bytes()) != digest:
            raise _packet_changed(f"The frozen review input {name} changed.")
    for name, asset in private.assets.items():
        if (
            name.startswith("_")
            and _hash((round_path / name).read_bytes()) != asset.sha256
        ):
            raise _packet_changed(f"The frozen structural input {name} changed.")
    return private, parsed


def _packet_changed(message: str) -> FlowError:
    return FlowError(
        f"{message} This round can no longer be reviewed.",
        code="review.packet_changed",
        action=_new_round_action(),
    )


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
            missing = [item for item in ids if item not in seen]
            unexpected = sorted(set(seen) - set(ids))
            duplicated = sorted({item for item in seen if seen.count(item) > 1})
            raise _report_fix(
                f"The report must judge every original {label} exactly once."
                + (f" Missing: {', '.join(missing)}." if missing else "")
                + (f" Unknown: {', '.join(unexpected)}." if unexpected else "")
                + (f" Repeated: {', '.join(duplicated)}." if duplicated else ""),
                fields=(f"{label}s",),
                missing=missing,
                unexpected=unexpected,
                duplicated=duplicated,
            )
    expected_reads = manifest["image_reads"]
    if sorted(report.reviewer.image_reads) != sorted(expected_reads):
        actual = report.reviewer.image_reads
        raise _report_fix(
            "reviewer.image_reads must list every target, render and overlay in the "
            "manifest, after opening each one.",
            fields=("reviewer.image_reads",),
            missing=[name for name in expected_reads if name not in actual],
            unexpected=[name for name in actual if name not in expected_reads],
        )
    if manifest["parent_session_id"] not in (None, report.reviewer.parent_session_id):
        raise _report_fix(
            "reviewer.parent_session_id differs from the session that prepared this round.",
            fields=("reviewer.parent_session_id",),
            expected=manifest["parent_session_id"],
        )
    width, height = manifest["image_pixels"]
    views = json.loads((packet / "metrics.json").read_bytes())["views"]
    for item in (*report.conditions, *report.features, *report.findings):
        for evidence in item.evidence:
            left, top, right, bottom = evidence.bbox
            if evidence.view not in views or not (
                0 <= left < right <= width and 0 <= top < bottom <= height
            ):
                item_id = item.description if isinstance(item, Finding) else item.id
                raise _report_fix(
                    f"Evidence for {item_id} must name one of the views "
                    f"{', '.join(views)} and a pixel box inside {width} x {height}.",
                    fields=("evidence",),
                    item=item_id,
                    view=evidence.view,
                    bbox=list(evidence.bbox),
                    views=list(views),
                    image_pixels=[width, height],
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
            raise _report_fix(
                f"{result.id} is marked simplified, but its feature records no agreed "
                "simplification.",
                fields=("features",),
                item=result.id,
            )
        if result.status in ("partial", "missing"):
            blocked |= feature["importance"] == "required"
            remaining.append(f"{result.id} {feature['name']} {result.repair}")
    for item in report.findings:
        if item.check_id is not None and item.check_id not in checks_by_id:
            raise _report_fix(
                f"A finding cites unknown condition {item.check_id}.",
                fields=("findings",),
                item=item.check_id,
                conditions=list(checks_by_id),
            )
        blocked |= item.required
        remaining.append(f"{item.description} {item.repair}")
    if blocked != (report.verdict == "needs_changes"):
        expected = "needs_changes" if blocked else "passed"
        raise _report_fix(
            f"The verdict must be {expected} given the reported conditions, required "
            "features and required findings.",
            fields=("verdict",),
            expected=expected,
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
        raise FlowError(
            "This independent reviewer session already reviewed another round.",
            code="review.session",
            action=edit(
                "Dispatch a fresh reviewer, put its session ID in reviewer.session_id, "
                "and submit the report again.",
                FILE,
                run("Submit the report.", "external", "review", RECORD, "--set", FILE),
                fields=("reviewer.session_id",),
            ),
            extra={"round_open": True},
        )


def _submit(record: Record, raw: bytes) -> dict:
    try:
        data = TypeAdapter(dict[str, Any]).validate_json(raw)
    except ValidationError as exc:
        raise _report_fix(
            "The report is not a JSON object.",
            fields=("$",),
            errors=validation_errors(exc),
        ) from None
    model = FailedReport if "failure_reason" in data else Report
    try:
        report = model.model_validate(data)
    except ValidationError as exc:
        errors = validation_errors(exc)
        raise _report_fix(
            validation_message("The report", errors),
            fields=tuple(dict.fromkeys(item["path"] for item in errors)),
            errors=errors,
        ) from None
    round_path = _round_path(record, report.round_id)
    if not (round_path / "bindings.json").is_file():
        raise _report_fix(
            f"No review round {report.round_id} exists for this record.",
            fields=("round_id",),
            round_id=report.round_id,
        )
    private, manifest = _read_manifest(round_path)
    if report.input_sha256 != private.manifest_sha256:
        raise _report_fix(
            "input_sha256 does not match this round's frozen inputs.",
            fields=("input_sha256",),
            expected=private.manifest_sha256,
        )
    report_data = report.model_dump(mode="json")
    destination = round_path / "review.json"
    if destination.is_file():
        existing = json.loads(destination.read_bytes())
        if existing["report"] != report_data:
            raise _round_closed()
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
            raise _round_closed()
        return existing
    if status == "accepted":
        _replace(record.path / "review.json", _json(result))
    return result


def _round_closed() -> FlowError:
    return FlowError(
        "This review round already has a different report and is closed.",
        code="review.round_closed",
        action=_new_round_action(),
    )


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


def review_progress(
    record: Record, snapshot_id: str | None, mpd_path: str | None
) -> dict:
    """Where the current snapshot stands between rendering and a submitted report."""
    progress: dict[str, Any] = {"render": None, "comparison": None, "open_round": None}
    try:
        render_bytes = _render_path(record).read_bytes()
        render = _Render.model_validate_json(render_bytes)
    except (OSError, ValueError):
        return progress
    try:
        model_digest = _hash(Path(mpd_path).read_bytes()) if mpd_path else None
    except OSError:
        model_digest = None
    cameras = _cameras_path(record, render)
    progress["render"] = {
        "alignment": render.alignment,
        "views": list(render.views),
        "cameras": str(cameras) if cameras else None,
        "current": bool(
            snapshot_id
            and render.snapshot_id == snapshot_id
            and render.source_sha256 == record.source_sha256
            and render.model_sha256 == model_digest
        ),
    }
    try:
        comparison = _Comparison.model_validate_json(
            _comparison_path(record).read_bytes()
        )
        references = [
            record.path / asset.path for asset in comparison.references.values()
        ]
        current = comparison.render.sha256 == _hash(render_bytes) and all(
            _hash(path.read_bytes()) == asset.sha256
            for path, asset in zip(
                references, comparison.references.values(), strict=True
            )
        )
        parents = {path.parent for path in references}
        progress["comparison"] = {
            "current": current,
            "reference_dir": str(parents.pop()) if len(parents) == 1 else None,
        }
    except (OSError, ValueError):
        pass
    rounds = sorted(
        (record.path / "reviews").glob("*/bindings.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for bindings in rounds:
        round_path = bindings.parent
        if (round_path / "review.json").exists():
            continue
        if review_inputs_current(record, round_path.name):
            progress["open_round"] = {
                "round_id": round_path.name,
                "report_path": str((round_path / "report.json").resolve()),
            }
            break
    return progress
