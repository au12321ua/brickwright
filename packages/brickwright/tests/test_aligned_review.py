import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from brickwright import aligned_review as review
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
    rounds = sorted(
        (record.path / "reviews").iterdir(), key=lambda path: path.stat().st_mtime_ns
    )
    packet = rounds[-1] / "inputs"
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


def test_native_review_sees_images_in_fresh_request_and_accounts_usage(reviewed_record):
    from types import SimpleNamespace

    from brickwright.config import Config, Effort

    record, _, _ = reviewed_record
    _, example = _open_round(record)
    judgment = {
        key: example[key] for key in ("conditions", "features", "findings", "verdict")
    }
    response = SimpleNamespace(
        id="critique-response",
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(judgment)))
        ],
    )

    def complete(*, messages, model, reasoning_effort):
        assert model == "separate-vision"
        assert reasoning_effort == "medium"
        assert [message["role"] for message in messages] == ["system", "user"]
        assert sum(item["type"] == "image_url" for item in messages[1]["content"]) == 6
        assert all(
            "create_build" not in item.get("text", "")
            for item in messages[1]["content"]
        )
        return response

    observed = []
    config = Config(
        model="builder", critique_model="separate-vision", critique_effort=Effort.MEDIUM
    )
    saved = review.run_independent_review(
        record, config, observed.append, complete=complete
    )
    assert observed == [response]
    assert saved["status"] == "accepted"
    assert saved["report"]["reviewer"]["dispatch"] == "independent_model"
    assert review.review_status(record, "first")["passed"]
