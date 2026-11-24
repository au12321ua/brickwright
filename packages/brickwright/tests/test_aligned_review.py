import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from brickwright import aligned_review as review
from brickwright.actions import RECORD, FlowError
from brickwright.record import Record


def test_compare_matches_verifier_thresholds_and_labels():
    reference = np.array(
        [
            [
                [10, 30, 70, 128],
                [255, 30, 70, 128],
                [10, 30, 70, 127],
                [10, 30, 70, 255],
                [10, 30, 70, 0],
            ]
        ],
        dtype=np.uint8,
    )
    candidate = np.array(
        [
            [
                [26, 30, 70, 255],
                [0, 30, 70, 255],
                [10, 30, 70, 128],
                [10, 30, 70, 127],
                [10, 30, 70, 127],
            ]
        ],
        dtype=np.uint8,
    )
    scores, overlay = review.compare_images(reference, candidate)
    assert scores["silhouette_iou"] == 0.5
    assert scores["rgb_agreement_over_union"] == 0.25
    assert {
        (region["kind"], tuple(region["bbox"]), region["area"])
        for region in scores["regions"]
    } == {
        ("color_mismatch", (1, 0, 2, 1), 1),
        ("candidate_only", (2, 0, 3, 1), 1),
        ("reference_only", (3, 0, 4, 1), 1),
    }
    assert overlay[0, 0, 3] > 0 and overlay[0, 4, 3] == 0
    assert len({tuple(pixel) for pixel in overlay[0, 1:4]}) == 3


def test_compare_difference_components_rank_actual_area_and_use_half_open_boxes():
    reference = np.zeros((4, 6, 4), dtype=np.uint8)
    reference[0, 1:3, 3] = 255
    reference[1:3, 2, 3] = 255
    reference[3, 3:6, 3] = 255
    scores, _ = review.compare_images(reference, np.zeros_like(reference))
    assert scores["regions"] == [
        {"kind": "reference_only", "bbox": [1, 0, 3, 3], "area": 4},
        {"kind": "reference_only", "bbox": [3, 3, 6, 4], "area": 3},
    ]


def test_compare_rejects_shape_mismatch_and_empty_union():
    image = np.zeros((2, 3, 4), dtype=np.uint8)
    with pytest.raises(ValueError, match="same dimensions"):
        review.compare_images(image, image[:, :2])
    with pytest.raises(ValueError, match="empty foreground union"):
        review.compare_images(image, image)


def cameras_spec():
    return {
        "image_pixels": [140, 100],
        "projection": "perspective",
        "views": {
            "top": {
                "eye": [0, -150, 0],
                "target": [0, 0, 0],
                "up": [1, 0, 0],
                "vertical_fov_degrees": 30,
            },
            "reverse": {
                "eye": [0, 0, 150],
                "target": [0, 0, 0],
                "up": [0, -1, 0],
                "vertical_fov_degrees": 25,
            },
        },
    }


def test_camera_validation():
    spec = cameras_spec()
    projection = review.Cameras.model_validate(spec)
    assert projection.image_pixels == (140, 100)
    assert tuple(projection.views) == ("top", "reverse")

    spec["views"]["top"]["up"] = [0, -1, 0]
    with pytest.raises(ValueError, match="parallel"):
        review.Cameras.model_validate(spec)
    spec = cameras_spec()
    spec["views"]["../unsafe"] = spec["views"]["top"]
    with pytest.raises(ValueError, match="view name"):
        review.Cameras.model_validate(spec)


def test_fixed_projection_renders_rgba_with_distinct_top_and_reverse(tmp_path):
    from brickwright.tools.rendering import render_available

    if not render_available():
        pytest.skip("requires the LDraw parts library")
    model = tmp_path / "build.ldr"
    model.write_text("0 Brick\n1 1 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n")
    spec = review.Cameras.model_validate(cameras_spec())
    paths = review.render_model(model, spec, tmp_path / "images")
    assert set(paths) == {"top", "reverse"}
    arrays = {}
    for name, path in paths.items():
        with Image.open(path) as image:
            assert image.mode == "RGBA"
            assert image.size == spec.image_pixels
            arrays[name] = np.asarray(image)
            assert arrays[name][0, 0, 3] == 0
            assert np.count_nonzero(arrays[name][..., 3] >= 128) > 100
    assert not np.array_equal(arrays["top"], arrays["reverse"])


@pytest.fixture
def reviewed_record(tmp_path, monkeypatch):
    record = Record(tmp_path / "record")
    structure = record.path / "flow/snapshots/first"
    structure.mkdir(parents=True)
    model = structure / "build.ldr"
    model.write_text("0 Checked model")
    (structure / "structure.json").write_text('{"passed": true}')
    record.build_path.write_text("def create_build(): pass\n")
    record.meta_path.write_text('{"prompt": "Build a blue object"}')
    task = record.path / "task.json"
    task.write_text('{"checks":["Retain the blue silhouette"]}')
    (record.path / "conditions.json").write_text(
        '{"checks":[{"id":"C01","text":"Retain the blue silhouette"}]}'
    )
    (record.path / "features.json").write_text(
        '{"features":[{"id":"F01","name":"Blue fuselage","category":"retain","importance":"required"}]}'
    )
    state = {
        "structural_passed": True,
        "snapshot_id": "first",
        "mpd_path": str(model),
        "artifacts": {
            name: {
                "valid": True,
                "sha256": review._hash((record.path / f"{name}.json").read_bytes()),
            }
            for name in ("conditions", "features")
        },
    }
    monkeypatch.setattr(review, "_structural_state", lambda rec: state)
    spec = cameras_spec()
    cameras = tmp_path / "cameras.json"
    cameras.write_text(json.dumps(spec))
    reference = tmp_path / "refs"
    reference.mkdir()

    def stub_render(fn, args, timeout):
        _, source, preview_mpd, parsed_spec, output = args
        assert source is None
        assert preview_mpd.read_bytes() == model.read_bytes()
        output.mkdir()
        rendered = {}
        for view in parsed_spec.views:
            image = Image.new("RGBA", parsed_spec.image_pixels, (0, 60, 210, 255))
            image.save(reference / f"goal-{view}.png")
            image.putpixel((0, 0), (240, 20, 50, 255))
            rendered[view] = output / f"{view}.png"
            image.save(rendered[view])
        return rendered, review._hash(model.read_bytes())

    monkeypatch.setattr(review, "run_in_subprocess", stub_render)
    render = review.render_cameras(record, cameras)
    assert render["snapshot_id"] == "first"
    assert (record.out_dir / "views/top.png").is_file()
    comparison = review.compare_record(record, reference)
    assert comparison["metrics"]["top"]["regions"][0]["bbox"] == [0, 0, 1, 1]
    return record, state, reference


def _open_round(record):
    instructions = review.review_instructions(record, parent_session_id="builder-42")
    assert "fresh, independent" in instructions
    rounds = (record.path / "reviews").glob("*/inputs/manifest.json")
    packet = max(rounds, key=lambda path: path.stat().st_mtime_ns).parent
    manifest = packet / "manifest.json"
    data = json.loads(manifest.read_bytes())
    assert "build.py" not in data["files"]
    assert "_build.ldr" not in data["files"]
    assert "task.json" in data["files"]
    assert "_structure.json" not in data["files"]
    assert review.review_inputs_current(record, data["round_id"])
    assert (packet / "task.json").stat().st_mode & 0o222 == 0
    return packet, {
        "schema_version": 1,
        "round_id": data["round_id"],
        "input_sha256": review._hash(manifest.read_bytes()),
        "reviewer": {
            "session_id": f"reader-{data['round_id']}",
            "parent_session_id": "builder-42",
            "model": "gpt-6.2-sol",
            "effort": "medium",
            "dispatch": "codex_subagent",
            "image_reads": data["image_reads"],
        },
        "conditions": [
            {
                "id": "C01",
                "status": "satisfied",
                "explanation": "Blue area matches",
                "evidence": [
                    {
                        "view": "top",
                        "bbox": [0, 0, 12, 12],
                        "observation": "Blue center",
                    }
                ],
            }
        ],
        "features": [
            {
                "id": "F01",
                "status": "retained",
                "evidence": [
                    {
                        "view": "reverse",
                        "bbox": [2, 2, 12, 12],
                        "observation": "Blue trim",
                    }
                ],
            }
        ],
        "findings": [],
        "verdict": "passed",
    }


def _submit(record, report, tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    return review.submit_review(record, path)


def test_frozen_packet_and_idempotent_review(reviewed_record, tmp_path):
    record, _, _ = reviewed_record
    _, report = _open_round(record)
    result = _submit(record, report, tmp_path)
    assert result["status"] == "accepted"
    assert _submit(record, report, tmp_path) == result
    assert review.review_status(record, "first") == {
        "passed": True,
        "rounds": 1,
        "remaining_items": [],
        "current": True,
        "dispatch": "codex_subagent",
    }
    report["features"][0]["evidence"][0]["observation"] = "Second conflicting answer"
    with pytest.raises(ValueError, match="different report"):
        _submit(record, report, tmp_path)


@pytest.mark.parametrize(
    "change", ["model", "features", "reference", "source", "camera"]
)
def test_stale_reports_do_not_grant_credit(reviewed_record, tmp_path, change):
    record, state, reference = reviewed_record
    packet, report = _open_round(record)
    frozen = (packet / "goal-top.png").read_bytes()
    files = {
        "model": Path(state["mpd_path"]),
        "features": record.path / "features.json",
        "reference": reference / "goal-top.png",
        "source": record.build_path,
        "camera": tmp_path / "cameras.json",
    }
    files[change].write_bytes(b"changed")
    assert _submit(record, report, tmp_path)["status"] == "stale"
    assert not review.review_inputs_current(record, report["round_id"])
    assert review.review_status(record, "first") == {
        "passed": False,
        "rounds": 0,
        "remaining_items": [],
        "current": False,
        "dispatch": None,
    }
    assert (packet / "goal-top.png").read_bytes() == frozen


def test_accepted_round_loses_currency_when_reference_changes(
    reviewed_record, tmp_path
):
    record, _, reference = reviewed_record
    _, report = _open_round(record)
    assert _submit(record, report, tmp_path)["status"] == "accepted"
    (reference / "goal-top.png").unlink()
    assert review.review_status(record, "first") == {
        "passed": False,
        "rounds": 1,
        "remaining_items": [],
        "current": False,
        "dispatch": None,
    }


def test_validate_reviews_and_failures(reviewed_record, tmp_path):
    record, _, _ = reviewed_record
    _, report = _open_round(record)
    report["reviewer"]["session_id"] = "builder-42"
    with pytest.raises(ValueError, match="separate session"):
        _submit(record, report, tmp_path)
    report["reviewer"]["session_id"] = "reviewer-abc"
    report["reviewer"]["image_reads"].pop()
    with pytest.raises(ValueError, match="every target"):
        _submit(record, report, tmp_path)
    failure = {
        key: report[key] for key in ("schema_version", "round_id", "input_sha256")
    }
    failure["failure_reason"] = "Independent reviewer could not read an overlay"
    assert _submit(record, failure, tmp_path)["status"] == "failed"
    assert review.review_status(record, "first")["rounds"] == 0


def test_requires_original_conditions_and_records_required_discovery(
    reviewed_record, tmp_path
):
    record, _, _ = reviewed_record
    _, report = _open_round(record)
    original = report["conditions"].pop()
    with pytest.raises(ValueError, match="every original condition"):
        _submit(record, report, tmp_path)
    report["conditions"].append(original)
    report["findings"] = [
        {
            "description": "Missing visible corner feature",
            "required": True,
            "evidence": original["evidence"],
            "repair": "Extend the blue corner",
        }
    ]
    with pytest.raises(ValueError, match="verdict"):
        _submit(record, report, tmp_path)
    report["verdict"] = "needs_changes"
    assert _submit(record, report, tmp_path)["status"] == "accepted"
    status = review.review_status(record, "first")
    assert status["current"] and not status["passed"]
    assert status["remaining_items"] == [
        "Missing visible corner feature Extend the blue corner"
    ]


def test_new_comparison_invalidates_outstanding_review_even_if_original_images_persist(
    reviewed_record, tmp_path
):
    record, _, reference = reviewed_record
    _, report = _open_round(record)
    replacement = tmp_path / "new-reference"
    replacement.mkdir()
    for path in reference.iterdir():
        (replacement / path.name).write_bytes(path.read_bytes())
    with Image.open(replacement / "goal-top.png") as image:
        image.putpixel((0, 0), (240, 60, 240, 255))
        image.save(replacement / "goal-top.png")
    review.compare_record(record, replacement)
    assert _submit(record, report, tmp_path)["status"] == "stale"


def test_new_review_cannot_reuse_reviewer_session(reviewed_record, tmp_path):
    record, _, _ = reviewed_record
    _, first = _open_round(record)
    assert _submit(record, first, tmp_path)["status"] == "accepted"
    _, next_round = _open_round(record)
    next_round["reviewer"]["session_id"] = first["reviewer"]["session_id"]
    with pytest.raises(ValueError, match="already reviewed another round"):
        _submit(record, next_round, tmp_path)
    assert review.review_status(record, "first")["rounds"] == 1


def test_external_review_can_fall_back_to_matched_viewpoint(
    reviewed_record, monkeypatch
):
    record, state, _ = reviewed_record
    (record.out_dir / "comparison.json").unlink()
    (record.out_dir / "aligned-render.json").unlink()
    portrait = record.path / "prompt.jpg"
    Image.new("RGB", (100, 200), (80, 220, 140)).save(portrait)
    record.meta_path.write_text(
        json.dumps({"prompt": "Build a blue object", "prompt_image": portrait.name})
    )
    instructions = review.review_instructions(record)
    assert "external render-angle" in instructions
    assert "--azimuth <A> --elevation <E>" in instructions
    assert (
        f"external review-instructions {record.id} --azimuth <A> --elevation <E> "
        f"--records-dir {record.path.parent}"
    ) in instructions

    def render_approximate(fn, args, timeout):
        model, angles, output = args
        assert model.read_bytes() == Path(state["mpd_path"]).read_bytes()
        assert (angles.azimuth, angles.elevation) == (45, 30)
        Image.new("RGBA", (90, 90), (72, 80, 200, 255)).save(output)

    monkeypatch.setattr(review, "run_in_subprocess", render_approximate)
    instructions = review.review_instructions(
        record, parent_session_id="builder", matched_angle=(45, 30)
    )
    assert "reference image's coordinate system" in instructions
    packet = next((record.path / "reviews").glob("*/inputs"))
    manifest = json.loads((packet / "manifest.json").read_bytes())
    assert manifest["alignment"] == "matched_viewpoint"
    assert manifest["image_reads"] == ["goal-matched.png", "candidate-matched.png"]
    assert manifest["image_pixels"] == [100, 200]
    assert not (packet / "overlay-matched.png").exists()
    with Image.open(packet / "goal-matched.png") as goal:
        assert goal.format == "PNG"
    metrics = json.loads((packet / "metrics.json").read_bytes())
    assert metrics["views"]["matched"]["pixel_scores"] is None

    receipt = {
        "schema_version": 1,
        "round_id": manifest["round_id"],
        "input_sha256": review._hash((packet / "manifest.json").read_bytes()),
        "reviewer": {
            "session_id": "critique-1",
            "parent_session_id": "builder",
            "model": "gpt-6.2-sol",
            "effort": "medium",
            "dispatch": "codex_subagent",
            "image_reads": manifest["image_reads"],
        },
        "conditions": [
            {
                "id": "C01",
                "status": "satisfied",
                "explanation": "Outline retained",
                "evidence": [
                    {
                        "view": "matched",
                        "bbox": [0, 0, 100, 200],
                        "observation": "Full frame",
                    }
                ],
            }
        ],
        "features": [
            {
                "id": "F01",
                "status": "retained",
                "evidence": [
                    {"view": "matched", "bbox": [0, 0, 100, 200], "observation": "Blue"}
                ],
            }
        ],
        "findings": [],
        "verdict": "passed",
    }
    assert _submit(record, receipt, record.path)["status"] == "accepted"
    assert review.review_status(record, "first")["passed"]
    record.meta_path.write_text(
        '{"prompt": "Build a blue object", "prompt_image": "second.png"}'
    )
    assert not review.review_status(record, "first")["current"]


def test_historical_review_count_requires_intact_frozen_packet(
    reviewed_record, tmp_path
):
    record, _, _ = reviewed_record
    packet, finished = _open_round(record)
    assert _submit(record, finished, tmp_path)["status"] == "accepted"
    assert review.review_status(record, "")["rounds"] == 1
    (packet / "goal-top.png").unlink()
    assert review.review_status(record, "")["rounds"] == 0


def test_host_self_review_can_use_the_build_session_for_multiple_rounds(
    reviewed_record, tmp_path
):
    record, _, _ = reviewed_record
    for number in (1, 2):
        _, report = _open_round(record)
        reviewer = report["reviewer"]
        reviewer.update(dispatch="self", session_id=reviewer["parent_session_id"])
        assert _submit(record, report, tmp_path)["passed"]
        status = review.review_status(record, "first")
        assert status["dispatch"] == "self"
        assert status["rounds"] == number


def test_self_review_must_identify_the_builder(reviewed_record, tmp_path):
    record, _, _ = reviewed_record
    _, report = _open_round(record)
    report["reviewer"]["dispatch"] = "self"
    with pytest.raises(ValueError, match="builder session"):
        _submit(record, report, tmp_path)


@pytest.mark.parametrize(
    "check_id,passed", [("C01_wing", True), ("C02_unrelated", False)]
)
def test_internal_condition_uses_frozen_validation_check(
    reviewed_record, tmp_path, check_id, passed
):
    record, state, _ = reviewed_record
    structure = Path(state["mpd_path"]).with_name("structure.json")
    pointer = record.path / "flow/current.json"
    pointer.write_text(
        json.dumps({"structure_sha256": review._hash(structure.read_bytes())})
    )
    state["artifacts"]["structure"] = {"sha256": review._hash(pointer.read_bytes())}
    archived = record.snapshots_dir / "0001/report.json"
    archived.parent.mkdir(parents=True)
    validation = {
        "ok": True,
        "report_path": str(archived.relative_to(record.path)),
        "source_sha256": record.source_sha256,
        "flow": state,
        "checks": [{"name": check_id, "passed": True}],
    }
    archived.write_text(json.dumps(validation))
    record.report_path.write_bytes(archived.read_bytes())
    _, report = _open_round(record)
    report["conditions"][0].update(
        status="structural_evidence_required",
        evidence=[],
        repair="Check internal joint",
    )
    receipt = _submit(record, report, tmp_path)
    assert receipt["passed"] is passed
    assert bool(receipt["remaining_items"]) is not passed
    if passed:
        assert receipt["structural_evidence"]["C01"] == [check_id]


def test_compare_names_each_missing_reference_image(reviewed_record, tmp_path):
    record, _, reference = reviewed_record
    (reference / "goal-reverse.png").rename(reference / "reverse.png")
    with pytest.raises(ValueError) as error:
        review.compare_record(record, reference)
    message = str(error.value)
    assert "goal-reverse.png" in message
    assert "goal-top.png" not in message
    assert "goal-<camera>.png" in message
    assert "top, reverse" in message


def test_uncompared_fixed_render_points_to_compare_not_viewpoint_matching(
    reviewed_record,
):
    record, _, _ = reviewed_record
    (record.out_dir / "comparison.json").unlink()
    portrait = record.path / "prompt.png"
    Image.new("RGB", (100, 100), (80, 220, 140)).save(portrait)
    record.meta_path.write_text(
        json.dumps({"prompt": "Build a blue object", "prompt_image": portrait.name})
    )
    with pytest.raises(FlowError) as error:
        review.review_instructions(record, parent_session_id="builder")
    assert error.value.code == "review.comparison_missing"
    assert "goal-top.png" in str(error.value)
    action = error.value.action
    assert action is not None and action.kind == "provide"
    assert action.then is not None
    assert action.then.args == (
        "external",
        "compare",
        RECORD,
        "--reference-dir",
        "REFERENCE_DIR",
    )
    assert "render-angle" not in str(error.value)
    assert not (record.path / "reviews").exists()
