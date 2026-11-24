"""A host can recover on the same record using only the returned next action."""

import json
import shlex
import sys
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from brickwright.cli import app

BUILD = """
from bricklayer import Color, Element, LegoBuild
from brickwright import TestContext


def create_build():
    build = LegoBuild()
    build.add(Element("3001", Color.RED), (0, 0, 0))
    build.add(Element("3001", Color.RED), (0, 0, 1))
    return build


def run_tests():
    checks = TestContext(create_build())
    checks.check_no_collisions()
    checks.check_no_floating()
    checks.check("C02_connected", checks.build.num_connected_components == 1)
    return checks.report()
"""

CAMERAS = {
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

PREFIX = [sys.executable, "-m", "brickwright"]


def invoke(argv: list[str]) -> tuple[int, dict]:
    """Run brickwright arguments in process and require exactly one JSON object."""
    result = CliRunner().invoke(app, argv)
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception
    )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return result.exit_code, payload


def execute(action: dict, **provided: str) -> tuple[int, dict]:
    """Execute a returned run action verbatim, substituting only provided inputs."""
    assert action["kind"] == "run", action
    argv = list(action["argv"])
    for placeholder, value in provided.items():
        assert placeholder in argv, (placeholder, argv)
        argv = [value if part == placeholder else part for part in argv]
    assert argv[:3] == PREFIX
    assert action["command"] == shlex.join(action["argv"])
    return invoke(argv[3:])


@pytest.fixture
def host(tmp_path, monkeypatch, in_process):
    """A task directory, an installed skill, and render fakes for fixed cameras."""
    from brickwright import aligned_review
    from brickwright.tools import rendering

    task = tmp_path / "task input"
    task.mkdir()
    (task / "task.json").write_text(
        json.dumps({"id": "stack", "checks": ["Use red.", "Connect both."]})
    )
    (task / "cameras.json").write_text(json.dumps(CAMERAS))
    Image.new("RGBA", (16, 16), (180, 32, 32, 255)).save(task / "goal-side.png")

    def render_frozen(model, _spec, output):
        output.mkdir()
        frame = output / "side.png"
        frame.write_bytes((task / "goal-side.png").read_bytes())
        return {"side": frame}

    monkeypatch.setattr(aligned_review, "render_model", render_frozen)
    monkeypatch.setattr(
        aligned_review, "run_in_subprocess", lambda fn, args, _: fn(*args)
    )
    monkeypatch.setattr(rendering, "render_available", lambda: False)
    monkeypatch.delenv("BRICKWRIGHT_RECORDS_DIR", raising=False)
    skill = tmp_path / "skill"
    installed = CliRunner().invoke(app, ["skill", "install", "--target", str(skill)])
    assert installed.exit_code == 0, installed.output
    return task, (skill / "SKILL.md").read_text()


def fill_features(path: Path) -> None:
    draft = json.loads(path.read_text())
    for feature in draft["features"]:
        feature.update(
            name=f"Keep {feature['id']}",
            description="Visible in the side view.",
            category="retain",
            importance="required",
            rationale="Original task condition.",
        )
    path.write_text(json.dumps(draft))


def fill_plan(path: Path) -> None:
    draft = json.loads(path.read_text())
    draft.update(bounds={"minimum": [0, 0, 0], "maximum": [80, 40, 48]}, colors=["red"])
    path.write_text(json.dumps(draft))


def review_report(
    path: Path, packet: Path, *, skip: str | None = None, add: tuple[str, ...] = ()
) -> None:
    """Act as the reviewer: judge every listed item from the frozen images."""
    report = json.loads(path.read_text())
    report["conditions"] += [
        {"id": item, "status": "", "explanation": "", "evidence": []} for item in add
    ]
    manifest = json.loads((packet / "manifest.json").read_text())
    evidence = [{"view": "side", "bbox": [0, 0, 16, 16], "observation": "Red stack."}]
    for condition in report["conditions"]:
        if condition["id"] == "C02":
            condition.update(
                status="structural_evidence_required",
                explanation="Contact is inside the assembly.",
                repair="Check that both bricks form one component.",
            )
        else:
            condition.update(
                status="satisfied",
                explanation="Both bricks are red.",
                evidence=evidence,
            )
    for feature in report["features"]:
        feature.update(status="retained", evidence=evidence)
    report["conditions"] = [item for item in report["conditions"] if item["id"] != skip]
    report["reviewer"].update(
        model="test", effort="test", image_reads=manifest["image_reads"]
    )
    report["verdict"] = "passed"
    path.write_text(json.dumps(report))


def test_host_repairs_a_faulty_receipt_on_the_same_round_and_delivers(host, tmp_path):
    task, skill = host
    assert "`next`" in skill
    records = tmp_path / "my records"
    code, created = invoke(
        [
            "external",
            "init",
            "Stack two red bricks",
            "--flow",
            "--task-json",
            str(task / "task.json"),
            "--records-dir",
            str(records),
        ]
    )
    assert code == 0 and created["ok"], created
    record_id = created["record_id"]
    record_dir = records / record_id
    step = created["next"]
    seen: list[str] = []
    submitted_rounds: list[str] = []
    rejected_once = False
    missing: list[str] = []
    built = False
    submission = tmp_path / "out dir" / "model.mpd"

    for _ in range(30):
        seen.append(step["kind"] + ":" + (step.get("summary") or ""))
        if step["kind"] == "edit":
            path = Path(step["path"])
            if path.name == "build.py":
                assert not built, "the host is asked to rebuild a passing model"
                path.write_text(BUILD)
                built = True
            elif path.parent.name == "drafts" and path.name == "features.json":
                fill_features(path)
            elif path.parent.name == "drafts" and path.name == "plan.json":
                fill_plan(path)
            else:
                assert path.name == "report.json", step
                round_dir = path.parent
                if not submitted_rounds:
                    review_report(path, round_dir / "inputs", skip="C02")
                else:
                    # Repair only what the rejection named, in the same file.
                    assert rejected_once and round_dir.name == submitted_rounds[0]
                    review_report(path, round_dir / "inputs", add=tuple(missing))
                submitted_rounds.append(round_dir.name)
            code, result = execute(step["then"])
        elif step["kind"] == "provide":
            then = step["then"]
            (placeholder,) = step["placeholders"]
            if "render" in then["argv"]:
                code, result = execute(
                    then, **{placeholder: str(task / "cameras.json")}
                )
            elif "compare" in then["argv"]:
                code, result = execute(then, **{placeholder: str(task)})
            else:
                assert "verify-delivery" in then["argv"], step
                submission.parent.mkdir()
                submission.write_bytes((record_dir / "out/build.ldr").read_bytes())
                code, result = execute(then, **{placeholder: str(submission)})
                assert code == 0 and result["ok"], result
                break
        else:
            code, result = execute(step)

        if not result["ok"]:
            assert code == 1
            issue = result["diagnostics"][0]
            assert issue["code"] == "review.report_invalid", result
            if not rejected_once:
                assert issue["evidence"]["missing"] == ["C02"]
                missing = issue["evidence"]["missing"]
            assert result["round_open"] is True
            rejected_once = True
        else:
            assert code == 0, result
        assert "next" in result, result
        step = result["next"]
    else:
        pytest.fail(f"no delivery after {seen}")

    assert rejected_once
    assert len(set(submitted_rounds)) == 1, submitted_rounds
    receipts = list((record_dir / "reviews").glob("*/review.json"))
    assert [path.parent.name for path in receipts] == submitted_rounds[:1]
    assert json.loads(receipts[0].read_text())["status"] == "accepted"
    assert sorted(path.name for path in records.iterdir()) == [record_id]
    snapshot = Path(result["mpd_path"]) if "mpd_path" in result else None
    assert result["snapshot_id"]
    frozen = record_dir / "flow/snapshots" / result["snapshot_id"] / "build.ldr"
    assert submission.read_bytes() == frozen.read_bytes()
    assert snapshot is None or snapshot == frozen


def _validated(task: Path, records: Path) -> tuple[str, Path]:
    """A flow record whose features, plan and build pass validation."""
    code, created = invoke(
        [
            "external",
            "init",
            "Stack",
            "--flow",
            "--task-json",
            str(task / "task.json"),
            "--records-dir",
            str(records),
        ]
    )
    record_id = created["record_id"]
    record_dir = records / record_id
    for name, fill in (("features", fill_features), ("plan", fill_plan)):
        draft = record_dir / "drafts" / f"{name}.json"
        fill(draft)
        code, _ = invoke(
            [
                "external",
                name,
                record_id,
                "--set",
                str(draft),
                "--records-dir",
                str(records),
            ]
        )
        assert code == 0
    (record_dir / "build.py").write_text(BUILD)
    code, result = invoke(
        ["external", "validate", record_id, "--records-dir", str(records)]
    )
    assert code == 0 and result["ok"], result
    return record_id, record_dir


def _cli(*args: str, records: Path) -> tuple[int, dict]:
    return invoke([*args, "--records-dir", str(records)])


def test_review_before_validation_points_to_the_first_open_step(host, tmp_path):
    task, _ = host
    records = tmp_path / "records"
    _, created = invoke(
        [
            "external",
            "init",
            "Stack",
            "--flow",
            "--task-json",
            str(task / "task.json"),
            "--records-dir",
            str(records),
        ]
    )
    record_id = created["record_id"]
    code, result = _cli("external", "review-instructions", record_id, records=records)
    assert code == 1
    assert result["diagnostics"][0]["code"] == "flow.structure"
    _, status = _cli("external", "status", record_id, records=records)
    assert result["next"] == status["next"]
    assert result["next"]["kind"] == "edit"
    assert result["next"]["path"].endswith("drafts/features.json")


def test_compare_before_render_and_missing_targets_ask_for_the_inputs(host, tmp_path):
    task, _ = host
    records = tmp_path / "records"
    record_id, _ = _validated(task, records)
    code, result = _cli(
        "external", "compare", record_id, "--reference-dir", str(task), records=records
    )
    assert code == 1
    assert result["diagnostics"][0]["code"] == "review.render_missing"
    step = result["next"]
    assert step["kind"] == "provide" and step["placeholders"] == ["CAMERAS_JSON"]
    code, rendered = execute(step["then"], CAMERAS_JSON=str(task / "cameras.json"))
    assert code == 0 and rendered["ok"]

    empty = tmp_path / "targets"
    empty.mkdir()
    code, result = _cli(
        "external", "compare", record_id, "--reference-dir", str(empty), records=records
    )
    assert code == 1
    issue = result["diagnostics"][0]
    assert issue["code"] == "review.references_missing"
    assert issue["evidence"]["missing"] == ["goal-side.png"]
    step = result["next"]
    assert step["placeholders"] == ["REFERENCE_DIR"]
    code, compared = execute(step["then"], REFERENCE_DIR=str(task))
    assert code == 0 and compared["ok"]
    assert compared["next"]["argv"][3:5] == ["external", "review-instructions"]


def _open_round(task: Path, records: Path) -> tuple[str, Path, dict]:
    record_id, record_dir = _validated(task, records)
    _cli(
        "external",
        "render",
        record_id,
        "--cameras",
        str(task / "cameras.json"),
        records=records,
    )
    _cli(
        "external", "compare", record_id, "--reference-dir", str(task), records=records
    )
    code, prepared = _cli("external", "review-instructions", record_id, records=records)
    assert code == 0, prepared
    return record_id, record_dir, prepared


def test_review_schema_and_compact_report_errors(host, tmp_path):
    from brickwright._review_reports import Report

    task, _ = host
    records = tmp_path / "records"
    record_id, record_dir, _ = _open_round(task, records)
    _, shown = _cli("external", "review", record_id, records=records)
    assert set(shown["schema"]["required"]) == set(
        Report.model_json_schema()["required"]
    )
    assert len(shown["schema"]["required"]) == 8
    assert "failure_reason" in shown["failed_attempt_schema"]["required"]

    empty = tmp_path / "my report.json"
    empty.write_text("{}")
    result = CliRunner().invoke(
        app,
        [
            "external",
            "review",
            record_id,
            "--set",
            str(empty),
            "--records-dir",
            str(records),
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert len(result.stdout) < 3000
    assert payload["error"].startswith("The report has 8 invalid fields")
    assert "pydantic" not in result.stdout
    issue = payload["diagnostics"][0]
    assert issue["code"] == "review.report_invalid"
    assert {item["path"] for item in issue["evidence"]["errors"]} >= {
        "verdict",
        "reviewer",
    }
    assert payload["round_open"] is True
    step = payload["next"]
    assert step["kind"] == "edit" and step["path"] == str(empty.absolute())
    assert str(empty.absolute()) in step["then"]["argv"]
    assert not list((record_dir / "reviews").glob("*/review.json"))


def test_stale_and_failed_receipts_do_not_count_as_review(host, tmp_path):
    task, _ = host
    records = tmp_path / "records"
    record_id, record_dir, prepared = _open_round(task, records)
    failed = tmp_path / "failed.json"
    failed.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "round_id": prepared["round_id"],
                "input_sha256": prepared["input_sha256"],
                "failure_reason": "The reviewer could not open the images.",
            }
        )
    )
    code, result = _cli(
        "external", "review", record_id, "--set", str(failed), records=records
    )
    assert code == 0 and result["receipt"] == "failed"
    assert result["counted"] is False and result["deliverable"] is False
    assert result["rounds"] == {"used": 0, "budget": 2, "remaining": 2}
    assert result["next"]["argv"][3:5] == ["external", "review-instructions"]

    code, again = execute(result["next"])
    assert code == 0
    report = Path(again["report_path"])
    review_report(report, Path(again["packet_dir"]))
    (record_dir / "build.py").write_text(BUILD.replace("(0, 0, 1)", "(0, 0, 1) "))
    _cli("external", "validate", record_id, records=records)
    code, stale = _cli(
        "external", "review", record_id, "--set", str(report), records=records
    )
    assert code == 1 and stale["receipt"] == "stale"
    assert stale["counted"] is False
    assert stale["diagnostics"][0]["code"] == "review.round_stale"
    assert stale["next"]["argv"][3:5] == ["external", "render"]


def test_delivery_rejections_name_the_repair(host, tmp_path):
    task, _ = host
    records = tmp_path / "records"
    record_id, record_dir = _validated(task, records)
    submission = tmp_path / "submitted model.mpd"
    submission.write_text("0 empty\n")
    code, early = _cli(
        "external", "verify-delivery", record_id, str(submission), records=records
    )
    assert code == 1
    assert early["diagnostics"][0]["code"] == "delivery.not_finalized"
    _, status = _cli("external", "status", record_id, records=records)
    assert early["next"] == status["next"]

    _, _, prepared = record_id, record_dir, None
    _cli(
        "external",
        "render",
        record_id,
        "--cameras",
        str(task / "cameras.json"),
        records=records,
    )
    _cli(
        "external", "compare", record_id, "--reference-dir", str(task), records=records
    )
    _, prepared = _cli("external", "review-instructions", record_id, records=records)
    review_report(Path(prepared["report_path"]), Path(prepared["packet_dir"]))
    _cli(
        "external",
        "review",
        record_id,
        "--set",
        prepared["report_path"],
        records=records,
    )
    code, final = _cli("external", "finalize", record_id, records=records)
    assert code == 0, final
    code, wrong = _cli(
        "external", "verify-delivery", record_id, str(submission), records=records
    )
    assert code == 1
    assert wrong["diagnostics"][0]["code"] == "delivery.mismatch"
    step = wrong["next"]
    assert step["kind"] == "provide" and step["path"] == str(submission.absolute())
    submission.write_bytes((record_dir / "out/build.ldr").read_bytes())
    code, accepted = execute(step["then"])
    assert code == 0 and accepted["ok"]


@pytest.mark.parametrize("command", ["validate", "render", "status"])
def test_unknown_record_prints_one_result_without_traceback(tmp_path, command):
    records = tmp_path / "my records"
    records.mkdir()
    args = ["external", command, "missing", "--records-dir", str(records)]
    if command == "render":
        args += ["--cameras", str(tmp_path / "cameras.json")]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    payload = json.loads(result.stdout)
    issue = payload["diagnostics"][0]
    assert issue["code"] == "record.not_found"
    assert issue["evidence"]["records_dir"] == str(records.absolute())
    assert payload["next"]["argv"][3:] == [
        "ls",
        "--records-dir",
        str(records.absolute()),
    ]


def test_custom_paths_survive_in_the_plan_repair(host, tmp_path):
    task, _ = host
    records = tmp_path / "records dir"
    _, created = invoke(
        [
            "external",
            "init",
            "Stack",
            "--flow",
            "--task-json",
            str(task / "task.json"),
            "--records-dir",
            str(records),
        ]
    )
    plan = tmp_path / "my plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bounds": {"minimum": [1, 0, 0], "maximum": [0, 1, 1]},
                "colors": [],
            }
        )
    )
    result = CliRunner().invoke(
        app,
        [
            "external",
            "plan",
            created["record_id"],
            "--set",
            str(plan),
            "--records-dir",
            str(records),
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert "schema" not in payload
    step = payload["next"]
    assert step["kind"] == "edit" and step["path"] == str(plan.absolute())
    assert step["then"]["argv"][3:] == [
        "external",
        "plan",
        created["record_id"],
        "--set",
        str(plan.absolute()),
        "--records-dir",
        str(records.absolute()),
    ]
    fill_plan(plan)
    code, accepted = execute(step["then"])
    assert code == 0 and accepted["ok"], accepted


def test_missing_ldraw_library_is_named_only_when_rendering(
    host, tmp_path, monkeypatch
):
    from brickwright import aligned_review
    from brickwright.tools.build_execution import ChildFailed

    task, _ = host
    records = tmp_path / "records"
    record_id, _ = _validated(task, records)
    code, _ = _cli(
        "external",
        "render",
        record_id,
        "--cameras",
        str(task / "cameras.json"),
        records=records,
    )
    assert code == 0

    def no_library(*_args):
        raise ChildFailed(
            "LDrawLibraryNotFound", "Could not find an LDraw parts library."
        )

    monkeypatch.setattr(aligned_review, "run_in_subprocess", no_library)
    code, compared = _cli(
        "external", "compare", record_id, "--reference-dir", str(task), records=records
    )
    assert code == 0, compared
    code, result = _cli(
        "external",
        "render",
        record_id,
        "--cameras",
        str(task / "cameras.json"),
        records=records,
    )
    assert code == 1
    assert result["diagnostics"][0]["code"] == "render.ldraw_missing"
    assert "LDRAW_LIBRARY_PATH" in result["next"]["summary"]
    assert result["next"]["then"]["argv"][3:6] == ["external", "render", record_id]


def test_worker_exceptions_reach_the_parent_with_type_and_message(capfd):
    from brickwright.tools.build_execution import ChildFailed, run_in_subprocess

    with pytest.raises(ChildFailed) as error:
        run_in_subprocess(json.loads, ("{",), 30)
    assert error.value.exception_type == "JSONDecodeError"
    assert "Expecting property name" in str(error.value)
    assert "Traceback" not in capfd.readouterr().err


def test_usage_errors_and_multiword_knowledge_queries(capsys):
    from brickwright.cli import main

    with pytest.raises(SystemExit) as stopped:
        main(["external", "validate"])
    assert stopped.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnostics"][0]["code"] == "cli.usage"
    assert payload["next"]["argv"][3:5] == ["external", "validate"]
    assert "--help" in payload["next"]["argv"]

    result = CliRunner().invoke(app, ["knowledge", "search", "hinge", "door"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["query"] == "hinge door"
