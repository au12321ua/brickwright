import json
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brickwright.config import Config
from brickwright.flow import flow_status, read_artifact, set_artifact
from brickwright.record import Record
from brickwright.tools.finalization import finalize
from brickwright.tools.validation import validate

STACK = """
from bricklayer import Color, Element, LegoBuild

def create_build():
    build = LegoBuild()
    build.add(Element("3001", Color.RED), (0, 0, 0))
    build.add(Element("3001", Color.RED), (0, 0, 1))
    return build

def run_tests():
    return []
"""

PLAN = {
    "schema_version": 1,
    "bounds": {"minimum": [0, 0, 0], "maximum": [80, 40, 48]},
    "colors": ["red"],
}

FEATURE = {
    "id": "F1",
    "name": "Stack",
    "category": "retain",
    "importance": "required",
    "description": "Two red bricks should connect.",
    "evidence": [{"check_id": "C01"}],
    "rationale": "Preserve the task conditions.",
}


def _record(tmp_path: Path):
    task = tmp_path / "task.json"
    task.write_text('{"id":"stack","checks":["Use red.","Connect both."]}')
    record = Record.create(Config(records_dir=tmp_path, flow=True), task_path=task)
    record.write_build(STACK)
    return record


def _features():
    return {
        "schema_version": 1,
        "features": [
            FEATURE,
            dict(FEATURE, id="F2", evidence=[{"check_id": "C02"}]),
        ],
    }


def _ready(record):
    assert set_artifact(record, "features", _features())["diagnostics"] == []
    assert set_artifact(record, "plan", PLAN)["diagnostics"] == []


def _codes(status):
    return {item["code"] for item in status["diagnostics"]}


def test_original_conditions_cannot_be_replaced_or_omitted(tmp_path):
    record = _record(tmp_path)
    original = json.loads((record.path / "conditions.json").read_bytes())
    assert original["checks"] == [
        {"id": "C01", "text": "Use red."},
        {"id": "C02", "text": "Connect both."},
    ]
    missing = set_artifact(
        record, "features", {"schema_version": 1, "features": [FEATURE]}
    )
    assert "flow.features" in _codes(missing)
    _ready(record)
    replaced = _features()
    replaced["checks"] = [{"id": "C01", "text": "Use green."}]
    assert "flow.features" in _codes(set_artifact(record, "features", replaced))
    assert read_artifact(record, "features")["content"]["features"][0] == FEATURE

    original["checks"][0]["text"] = "Use green."
    (record.path / "conditions.json").write_text(json.dumps(original))
    assert "flow.conditions" in _codes(flow_status(record))


def test_validation_pass_and_review_gate_with_legacy_compatibility(tmp_path):
    record = _record(tmp_path)
    _ready(record)
    result = validate(record)
    assert result.ok, result
    state = flow_status(record)
    assert state["structural_passed"]
    mpd = Path(state["mpd_path"])
    assert mpd.is_file()
    assert "flow.review" in _codes(state)
    assert result.flow is not None
    assert state["snapshot_id"] == result.flow["snapshot_id"]
    summary = json.loads(result.agent_json())
    assert "structure" not in summary
    assert summary["flow"]["structure"]["counts"]["part_count"] == [2, 2]
    assert (record.path / summary["flow"]["structure"]["report_path"]).is_file()
    blocked = finalize(record)
    assert not blocked.ok
    assert "flow.review" in _codes(blocked.flow)
    assert not record.ldr_path.exists()

    legacy = Record.create(Config(records_dir=tmp_path, flow=False))
    legacy.write_build(STACK)
    assert finalize(legacy).ok
    assert legacy.ldr_path.is_file()


def test_layout_change_and_manifest_change_invalidate_structure(tmp_path):
    record = _record(tmp_path)
    _ready(record)
    result = validate(record)
    assert result.ok
    original = flow_status(record)
    set_artifact(
        record,
        "features",
        dict(
            _features(),
            features=[dict(FEATURE, description="Reword"), _features()["features"][1]],
        ),
    )
    assert flow_status(record)["snapshot_id"] == original["snapshot_id"]
    (record.path / "merge-manifest.json").write_text("{}")
    assert not flow_status(record)["structural_passed"]
    (record.path / "merge-manifest.json").unlink()
    set_artifact(record, "plan", dict(PLAN, colors=["blue"]))
    assert not flow_status(record)["structural_passed"]


def test_unknown_disconnected_component_prevents_delivery(tmp_path):
    record = _record(tmp_path)
    _ready(record)
    record.write_build(STACK.replace("(0, 0, 1)", "(6, 0, 0)"))
    report = validate(record)
    assert report.ok, report
    state = flow_status(record)
    assert not state["structural_passed"]
    assert "structure.unattached_component" in _codes(state)
    assert not finalize(record).ok


def test_missing_artifacts_are_described_for_the_builder(tmp_path):
    record = _record(tmp_path)
    response = read_artifact(record, "features")
    assert response["conditions"]["checks"][1] == {"id": "C02", "text": "Connect both."}
    assert response["content"] is None
    assert response["diagnostics"] == []
    assert "flow.features" in _codes(flow_status(record))


def test_current_review_publishes_exact_bytes_and_source_edits_expire_it(
    tmp_path,
    monkeypatch,
    in_process,
):
    from brickwright.flow import integrations, state
    from brickwright.tools import rendering

    record = _record(tmp_path)
    _ready(record)
    validate(record)
    checked = flow_status(record)["snapshot_id"]
    monkeypatch.setattr(rendering, "render_available", lambda: False)
    monkeypatch.setattr(integrations, "delivery_review_current", lambda _record: True)
    monkeypatch.setattr(
        state,
        "review_status",
        lambda _record, snapshot_id: {
            "passed": True,
            "rounds": 1,
            "remaining_items": [],
            "current": snapshot_id == checked,
        },
    )
    result = finalize(record)
    assert result.ok, result
    assert result.flow is not None and result.flow["finalized"]
    assert result.flow["review_outcome"] == "passed"
    assert record.ldr_path.read_bytes() == Path(result.flow["mpd_path"]).read_bytes()
    (record.path / "merge-manifest.json").write_text("changed")
    assert not record.read_meta().finalized
    (record.path / "merge-manifest.json").unlink()
    record.write_build(STACK + "\n# Source change\n")
    result = finalize(record)
    assert not result.ok
    assert not record.ldr_path.exists()


@pytest.fixture
def delivery_record(tmp_path, in_process, monkeypatch):
    from brickwright.flow import integrations, state
    from brickwright.tools import rendering

    record = _record(tmp_path)
    _ready(record)
    monkeypatch.setattr(rendering, "render_available", lambda: False)
    monkeypatch.setattr(integrations, "delivery_review_current", lambda _record: True)
    monkeypatch.setattr(
        state,
        "review_status",
        lambda _record, snapshot_id: {
            "passed": True,
            "rounds": 1,
            "remaining_items": [],
            "current": bool(snapshot_id),
            "dispatch": "self",
        },
    )
    return record


def test_delivery_cli_accepts_a_self_reviewed_copy(tmp_path, in_process, monkeypatch):
    from PIL import Image

    from brickwright import aligned_review
    from brickwright.cli import app
    from brickwright.tools import rendering, verify_delivery

    record = _record(tmp_path)
    _ready(record)
    record.write_build(
        STACK.replace(
            "    return []",
            "    from brickwright import TestContext\n"
            "    checks = TestContext(create_build())\n"
            "    checks.check('C02_connected', checks.build.num_connected_components == 1)\n"
            "    return checks.report()",
        )
    )
    checked = validate(record)
    assert checked.ok and checked.flow is not None and checked.report_path is not None
    frozen_mpd = Path(checked.flow["mpd_path"])
    archived_report = (record.path / checked.report_path).read_bytes()

    cameras = tmp_path / "cameras.json"
    cameras.write_text(
        json.dumps(
            {
                "image_pixels": [16, 16],
                "projection": "perspective",
                "views": {
                    "side": {
                        "eye": [0, 0, 120],
                        "target": [0, 0, 0],
                        "up": [0, -1, 0],
                        "vertical_fov_degrees": 30,
                    }
                },
            }
        )
    )
    target = tmp_path / "goal-side.png"
    Image.new("RGBA", (16, 16), (180, 32, 32, 255)).save(target)

    def render_frozen(model, _spec, output):
        assert model.read_bytes() == frozen_mpd.read_bytes()
        output.mkdir()
        frame = output / "side.png"
        frame.write_bytes(target.read_bytes())
        return {"side": frame}

    monkeypatch.setattr(aligned_review, "render_model", render_frozen)
    monkeypatch.setattr(
        aligned_review, "run_in_subprocess", lambda fn, args, _: fn(*args)
    )
    monkeypatch.setattr(rendering, "render_available", lambda: False)
    aligned_review.render_cameras(record, cameras)
    aligned_review.compare_record(record, tmp_path)
    aligned_review.review_instructions(record, parent_session_id="builder")
    manifest_bytes = next(
        (record.path / "reviews").glob("*/inputs/manifest.json")
    ).read_bytes()
    manifest = json.loads(manifest_bytes)
    evidence = [{"view": "side", "bbox": [0, 0, 16, 16], "observation": "Red stack."}]
    receipt = tmp_path / "review.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "round_id": manifest["round_id"],
                "input_sha256": sha256(manifest_bytes).hexdigest(),
                "reviewer": {
                    "session_id": "builder",
                    "parent_session_id": "builder",
                    "model": "test",
                    "effort": "test",
                    "dispatch": "self",
                    "image_reads": manifest["image_reads"],
                },
                "conditions": [
                    {
                        "id": "C01",
                        "status": "satisfied",
                        "explanation": "Both bricks are red.",
                        "evidence": evidence,
                    },
                    {
                        "id": "C02",
                        "status": "structural_evidence_required",
                        "explanation": "Contact is inside the assembly.",
                        "evidence": [],
                        "repair": "Check that both bricks form one component.",
                    },
                ],
                "features": [
                    {"id": feature_id, "status": "retained", "evidence": evidence}
                    for feature_id in ("F1", "F2")
                ],
                "findings": [],
                "verdict": "passed",
            }
        )
    )
    review = aligned_review.submit_review(record, receipt)
    assert review["passed"]
    assert review["structural_evidence"] == {"C02": ["C02_connected"]}
    result = finalize(record)
    assert result.ok and result.flow is not None
    assert record.report_path.read_bytes() != archived_report
    assert aligned_review.review_inputs_current(record, manifest["round_id"])
    submission = tmp_path / "submission.mpd"
    submission.write_bytes(record.ldr_path.read_bytes())
    sdk = verify_delivery(record, submission)
    assert sdk.ok
    assert sdk.review_dispatch == "self"
    command = [
        "external",
        "verify-delivery",
        record.id,
        str(submission),
        "--records-dir",
        str(tmp_path),
    ]

    cli = CliRunner()
    accepted = cli.invoke(app, command)
    assert accepted.exit_code == 0, accepted.output
    report = json.loads(accepted.stdout)
    assert report["ok"]
    assert report["snapshot_id"] == result.flow["snapshot_id"]
    assert report["mpd_sha256"] == manifest["model_sha256"]
    assert report["review_outcome"] == "passed"
    assert report["review_dispatch"] == "self"

    with submission.open("ab") as file:
        file.write(b"\n")
    rejected = cli.invoke(app, command)
    assert rejected.exit_code == 1
    assert json.loads(rejected.stdout)["ok"] is False
    submission.write_bytes(record.ldr_path.read_bytes())
    target.unlink()
    assert not verify_delivery(record, submission).ok


def test_delivery_requires_finalize_even_if_checked_bytes_were_copied(delivery_record):
    from brickwright.tools.finalization import verify_delivery

    result = validate(delivery_record)
    assert result.ok and result.flow is not None and result.flow["ready"]
    frozen = Path(result.flow["mpd_path"])
    delivery_record.ldr_path.write_bytes(frozen.read_bytes())
    assert not verify_delivery(delivery_record, frozen).ok


def test_delivery_requires_a_submission_and_a_flow(delivery_record, tmp_path):
    from brickwright.tools.finalization import verify_delivery

    assert finalize(delivery_record).ok
    assert not verify_delivery(delivery_record, tmp_path / "missing.mpd").ok
    legacy = Record.create(Config(records_dir=tmp_path, flow=False))
    legacy.write_build(STACK)
    assert finalize(legacy).ok
    assert not verify_delivery(legacy, legacy.ldr_path).ok


@pytest.mark.parametrize(
    "corrupted",
    [
        "build.py",
        "conditions.json",
        "out/build.ldr",
        "flow/current.json",
        "snapshot-mpd",
        "snapshot-report",
        "missing-snapshot",
    ],
)
def test_delivery_rejects_changed_evidence(delivery_record, corrupted, tmp_path):
    from brickwright.tools.finalization import verify_delivery

    result = finalize(delivery_record)
    assert result.ok and result.flow is not None
    frozen = Path(result.flow["mpd_path"])
    submission = tmp_path / "submission.mpd"
    submission.write_bytes(frozen.read_bytes())
    if corrupted == "missing-snapshot":
        frozen.unlink()
    else:
        path = {
            "snapshot-mpd": frozen,
            "snapshot-report": frozen.with_name("structure.json"),
        }.get(corrupted, delivery_record.path / corrupted)
        path.write_bytes(path.read_bytes() + b"\n")
    assert not verify_delivery(delivery_record, submission).ok


def test_delivery_rejects_previous_version_and_expired_catalog(
    delivery_record, tmp_path, monkeypatch
):
    from brickwright.flow import state
    from brickwright.tools.finalization import verify_delivery

    assert finalize(delivery_record).ok
    previous = tmp_path / "previous.mpd"
    previous.write_bytes(delivery_record.ldr_path.read_bytes())
    delivery_record.write_build(STACK.replace("Color.RED", "Color.BLUE"))
    assert not verify_delivery(delivery_record, previous).ok
    assert finalize(delivery_record).ok
    assert not verify_delivery(delivery_record, previous).ok
    assert verify_delivery(delivery_record, delivery_record.ldr_path).ok
    monkeypatch.setattr(state, "catalog_sha256", lambda: "new catalog")
    assert not verify_delivery(delivery_record, delivery_record.ldr_path).ok


def test_delivery_rejects_after_failed_refinalize(
    delivery_record, tmp_path, monkeypatch
):
    from brickwright.tools import rendering
    from brickwright.tools.finalization import verify_delivery

    assert finalize(delivery_record).ok
    submission = tmp_path / "submission.mpd"
    submission.write_bytes(delivery_record.ldr_path.read_bytes())
    monkeypatch.setattr(rendering, "render_available", lambda: True)

    def fail(*_):
        raise RuntimeError("render unavailable")

    monkeypatch.setattr(rendering, "_convert", fail)
    assert not finalize(delivery_record).ok
    assert not verify_delivery(delivery_record, submission).ok


def test_finalize_rejects_a_changed_gate_during_publication(
    delivery_record, monkeypatch
):
    from brickwright.tools import rendering

    def edit_features(*_):
        path = delivery_record.path / "features.json"
        path.write_bytes(path.read_bytes() + b"\n")

    monkeypatch.setattr(rendering, "render_available", lambda: True)
    monkeypatch.setattr(rendering, "_convert", edit_features)
    monkeypatch.setattr(rendering, "mesh2png", lambda *_: None)
    result = finalize(delivery_record)
    assert not result.ok
    assert not delivery_record.read_meta().finalized
    assert not delivery_record.ldr_path.exists()


def test_only_positive_round_budget_and_listed_issues_permit_delivery(
    tmp_path,
    monkeypatch,
    in_process,
):
    from brickwright.flow import integrations, state
    from brickwright.tools import rendering

    record = _record(tmp_path)
    _ready(record)
    review = {"passed": False, "rounds": 2, "remaining_items": [], "current": True}
    monkeypatch.setattr(state, "review_status", lambda *_: review)
    monkeypatch.setattr(rendering, "render_available", lambda: False)
    monkeypatch.setattr(integrations, "delivery_review_current", lambda _record: True)
    assert not finalize(record).ok
    review["remaining_items"] = ["Check the rear corner detail."]
    result = finalize(record)
    assert result.ok, result
    assert (
        result.flow is not None and result.flow["review_outcome"] == "rounds_exhausted"
    )
    record.update_meta(flow_review_rounds=0)
    assert not finalize(record).ok


def test_standard_reread_reports_unrepresentable_geometry():
    from bricklayer import Color, Element, LegoBuild, to_ldraw

    from brickwright.flow.structure import (
        classify_structure,
        inspect_build,
        inspect_mpd,
    )

    build = LegoBuild()
    build.add(Element("3001", Color.RED))
    bad_mpd = to_ldraw(build).encode().replace(b"3001.dat", b"unrepresentable.dat")
    standard, issues = inspect_mpd(bad_mpd)
    report = classify_structure(
        inspect_build(build), standard, bad_mpd, diagnostics=issues
    )
    assert not report.passed and not report.complete
    assert standard.part_count == 0
    assert any(issue.code == "structure.incomplete" for issue in report.diagnostics)


def test_declared_independent_object_can_be_larger_than_the_primary(
    tmp_path, in_process
):
    record = _record(tmp_path)
    _ready(record)
    record.write_build(
        STACK.replace("(0, 0, 1)", "(6, 0, 0)").replace(
            "    return build",
            '    build.add(Element("3001", Color.RED), (6, 0, 1))\n    return build',
        )
    )
    result = validate(record)
    assert result.structure is not None and result.structure.native is not None
    column = result.structure.native
    accessory = max(column.components, key=lambda component: component.part_count)
    members = [
        column.instances[index].model_dump(mode="json", exclude={"color_id", "path"})
        for index in accessory.members
    ]
    plan = dict(
        PLAN,
        independent_objects=[
            {"id": "accessory", "purpose": "Park separately.", "members": members}
        ],
    )
    assert set_artifact(record, "plan", plan)["diagnostics"] == []
    result = validate(record)
    assert result.flow is not None and result.flow["structural_passed"]


def test_catalog_revision_expires_the_current_structure(
    tmp_path, monkeypatch, in_process
):
    from brickwright.flow import state

    record = _record(tmp_path)
    _ready(record)
    assert validate(record).ok
    assert flow_status(record)["structural_passed"]
    monkeypatch.setattr(state, "catalog_sha256", lambda: "different catalog")
    assert not flow_status(record)["structural_passed"]


def test_validation_binds_module_motion_to_child_checks(
    config, monkeypatch, in_process
):
    from brickwright.tools import validation

    record = Record.create(config)
    record.write_build(STACK)
    motion = {"joints": [{"axis": "test"}]}
    monkeypatch.setattr(validation, "check_module", lambda *_: ("contract", []))
    monkeypatch.setattr(validation, "capture_module_motion", lambda *_: motion)

    report = validate(record)
    assert report.ok and report.module_motion == motion
    assert report.module_mpd_sha256
    assert json.loads(record.snapshots[-1].read_report())["module_motion"] == motion
    assert json.loads(report.agent_json()) == report.model_dump(mode="json")
