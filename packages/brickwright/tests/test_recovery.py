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
