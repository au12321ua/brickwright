"""The facts available from the workflow API and CLI after execution failures."""

import json
from hashlib import sha256

import pytest
from typer.testing import CliRunner

from brickwright.cli import app
from brickwright.tools import explore, finalize, rendering, validate
from brickwright.tools.validation import ValidationReport


def _external(record, config, command, *args):
    result = CliRunner().invoke(
        app,
        [
            "external",
            command,
            record.id,
            "--records-dir",
            str(config.records_dir),
            *args,
        ],
    )
    assert result.exit_code == 1, result.output
    return json.loads(result.output)


def _as_cli(payload):
    """The library payload as the CLI prints it: one copy of each message, plus next."""
    if isinstance(payload, list):
        return [_as_cli(item) for item in payload]
    if not isinstance(payload, dict):
        return payload
    payload = {key: _as_cli(value) for key, value in payload.items()}
    evidence = payload.get("evidence")
    if isinstance(evidence, dict) and evidence.get("exception_message") == payload.get(
        "message"
    ):
        payload["evidence"] = {
            key: value for key, value in evidence.items() if key != "exception_message"
        }
    return payload


def _without_next(cli):
    assert "next" in cli
    return {key: value for key, value in cli.items() if key != "next"}


@pytest.mark.parametrize(
    "source,stage,code,line",
    [
        ("def create_build(:\n    pass\n", "load_build", "source.syntax_error", 1),
        (
            "import nonexistent_diagnostic_fixture\n",
            "load_build",
            "source.import_error",
            1,
        ),
        (
            "def create_build():\n    raise RuntimeError('original')\n",
            "create_build",
            "execution.exception",
            2,
        ),
        (
            "def create_build():\n    return None\ndef run_tests():\n    raise RuntimeError('original')\n",
            "run_tests",
            "execution.exception",
            4,
        ),
    ],
)
def test_execution_provenance_matches_cli(
    make_record, config, in_process, source, stage, code, line
):
    record = make_record(source)
    payload = json.loads(validate(record, config).agent_json())
    cli = _external(record, config, "validate")
    assert cli["diagnostics"] == _as_cli(payload["diagnostics"])
    (issue,) = cli["diagnostics"]
    assert (issue["stage"], issue["code"]) == (stage, code)
    assert issue["evidence"]["source"]["file"] == "build.py"
    assert issue["evidence"]["source"]["line"] == line
    assert issue["evidence"]["source"]["text"] == source.splitlines()[line - 1]
    assert issue["evidence"]["exception_type"] == cli["error_type"]
    assert issue["message"]
    assert cli["checks"] == []
    archived = config.records_dir / record.id / cli["report_path"]
    assert (
        sha256(archived.with_name("build.py").read_bytes()).hexdigest()
        == cli["source_sha256"]
    )
    assert _as_cli(json.loads(archived.read_text())) == _without_next(cli)


@pytest.mark.parametrize(
    "snippet,file,line,stage",
    [
        ("\nraise ValueError('snippet problem')", "<explore>", 2, "explore"),
        ("print(build)", "build.py", 2, "create_build"),
    ],
)
def test_explore_identifies_the_source_that_failed(
    make_record, config, in_process, snippet, file, line, stage
):
    record = make_record("def create_build():\n    raise ValueError('build problem')\n")
    report = explore(record, snippet, config)
    cli = _external(record, config, "explore", "--code", snippet)
    assert _without_next(cli) == _as_cli(report.model_dump(mode="json"))
    (issue,) = cli["diagnostics"]
    assert issue["evidence"]["source"]["file"] == file
    assert issue["evidence"]["source"]["line"] == line
    assert issue["stage"] == stage


def test_connector_ambiguity_carries_actual_candidates(make_record, config, in_process):
    record = make_record("""import bricklayer as bl
def create_build():
    return bl.Element("3001", bl.Color.RED).find_connector("stud")
""")
    cli = _external(record, config, "validate")
    (issue,) = cli["diagnostics"]
    assert issue["code"] == "connector.ambiguous"
    assert issue["stage"] == "create_build"
    assert issue["evidence"]["matches"] == 8
    assert issue["evidence"]["part_id"] == "3001"
    assert len(issue["evidence"]["candidates"]) == 8
    assert "toward" in issue["next_action"]


_MOTION_BUILD = """import bricklayer as bl
from brickwright import TestContext

def create_build():
    model = bl.LegoBuild()
    base = model.add(bl.Element("3680", bl.Color.WHITE), name="base")
    top = model.connect(bl.Element("3679", bl.Color.RED), "turntable_pin",
                        to=base.find_connector("turntable_socket"),
                        motion=bl.MotionRange(bl.Revolute(0), bl.Revolute(180)),
                        name="top")
    model.stack(bl.plate(6, 1), on=top, rotation=bl.Rotation.DEG_90, name="arm")
    joint = model.joint(top)
    middle = model.posed({joint: bl.Revolute(90)})
    point = middle.named("arm").to_world(bl.Position(2.5, 0, 0))
    model.add_raw(bl.brick(1, 1), bl.Pose(translation=point), name="obstacle")
    return model

def run_tests():
    ctx = TestContext(create_build())
    ctx.check_motion(samples=3)
    return ctx.report()
"""


def test_finalize_feedback_includes_sampled_motion_and_joint_evidence(
    make_record, config, in_process
):
    record = make_record(_MOTION_BUILD)
    report = finalize(record, config)
    cli = _external(record, config, "finalize")
    assert not report.ok
    assert not record.ldr_path.exists()
    issues = cli["validation"]["diagnostics"]
    assert issues == json.loads(report.agent_json())["validation"]["diagnostics"]
    collision = next(issue for issue in issues if issue["code"] == "motion.collision")
    assert collision["stage"] == "check_motion"
    (sample,) = collision["evidence"]["samples"]
    assert {tuple(part["path"]) for part in sample["placements"]} == {
        ("arm",),
        ("obstacle",),
    }
    (joint,) = sample["positions"]
    assert joint["coordinates"]["angle"] == 90
    assert joint["parent"]["path"] == ["base"]
    assert joint["child"]["path"] == ["top"]


def test_export_failure_distinguishes_successful_validation(
    record, config, in_process, monkeypatch
):
    monkeypatch.setattr(rendering, "render_available", lambda: False)

    def fail(*args):
        raise OSError("export fixture failed")

    monkeypatch.setattr(rendering, "build_ldraw", fail)
    report = finalize(record, config)
    cli = _external(record, config, "finalize")
    assert cli["validation"]["ok"] and not cli["ok"]
    (issue,) = cli["diagnostics"]
    assert (issue["stage"], issue["code"]) == ("export", "output.export_failed")
    assert issue["message"].endswith("export fixture failed")
    assert issue["evidence"]["path"] == "out/build.ldr"
    assert _as_cli(report.model_dump(mode="json")["diagnostics"]) == cli["diagnostics"]
    assert not record.ldr_path.exists()


@pytest.mark.integration
def test_timeout_and_abnormal_exit_include_worker_facts(
    make_record, config, infinite_build
):
    record = make_record(infinite_build)
    timeout = _external(record, config, "validate", "--timeout", "0.2")
    (issue,) = timeout["diagnostics"]
    assert issue["code"] == "execution.timeout"
    assert issue["evidence"] == {"operation": "validate", "timeout_seconds": 0.2}
    record = make_record("import os\ndef create_build():\n    os._exit(28)\n")
    exit_ = _external(record, config, "validate")
    (issue,) = exit_["diagnostics"]
    assert issue["code"] == "execution.child_exit"
    assert issue["evidence"]["exit_code"] == 28


def test_older_validation_reports_remain_readable():
    report = ValidationReport.model_validate_json(
        '{"ok":false,"checks":[{"name":"no_collisions","passed":false,"detail":"old"}]}'
    )
    assert report.failed_checks == ["no_collisions"]
    assert report.diagnostics == []


def test_fixing_an_ambiguous_end_recovers_and_keeps_original_failure(
    make_record, config, in_process, monkeypatch
):
    source = """import bricklayer as bl

def create_build():
    model = bl.LegoBuild()
    base = model.add(bl.brick(2, 1), name="base")
    connector = base.find_connector("stud")
    model.connect(bl.brick(1, 1), "antistud", to=connector, name="top")
    return model

def run_tests():
    return []
"""
    record = make_record(source)
    failed = _external(record, config, "finalize")
    (issue,) = failed["validation"]["diagnostics"]
    assert issue["code"] == "connector.ambiguous"
    assert issue["evidence"]["matches"] == 2
    assert not record.ldr_path.exists()
    old_report = record.snapshots[0].report_path.read_text()

    record.build_path.write_text(
        source.replace(
            'base.find_connector("stud")',
            'base.find_connector("stud", toward=bl.Position(0.5, 0.5, 1))',
        )
    )
    monkeypatch.setattr(rendering, "render_available", lambda: False)
    result = CliRunner().invoke(
        app,
        ["external", "finalize", record.id, "--records-dir", str(config.records_dir)],
    )
    assert result.exit_code == 0, result.output
    assert record.ldr_path.exists()
    assert record.snapshots[0].read_build() == source
    assert record.snapshots[0].report_path.read_text() == old_report


def test_changed_source_preserves_which_line_really_ran(
    make_record, config, in_process
):
    original = """from pathlib import Path
def create_build():
    Path(__file__).write_text("# Replacement source")
    raise RuntimeError("from the original")
"""
    record = make_record(original)
    payload = _external(record, config, "validate")
    changed, failure = payload["diagnostics"]
    assert changed["code"] == "source.changed"
    assert changed["evidence"]["current_sha256"] == record.source_sha256
    assert (
        failure["evidence"]["source"]["text"]
        == '    raise RuntimeError("from the original")'
    )
    assert payload["error_type"] == "SourceChanged"
    assert 'raise RuntimeError("from the original")' in payload["build_error"]
    assert record.snapshots[0].read_build() == original


def test_render_library_failure_is_actionable_through_both_entries(
    record, config, in_process, monkeypatch
):
    from ldraw2mesh.library import LDrawLibraryNotFound

    def fail(*args):
        raise LDrawLibraryNotFound("library fixture missing")

    monkeypatch.setattr(rendering, "_convert", fail)
    report = rendering.render(record, config)
    cli = _external(record, config, "render")
    (issue,) = cli["diagnostics"]
    assert (issue["stage"], issue["code"]) == ("render", "output.ldraw_library_missing")
    assert issue["evidence"]["environment_variable"] == "LDRAW_LIBRARY_PATH"
    assert "LDRAW_LIBRARY_PATH" in issue["next_action"]
    assert _as_cli(report.model_dump(mode="json")["diagnostics"]) == cli["diagnostics"]


def test_static_check_evidence_names_repeated_submodel_occurrences():
    from bricklayer import LegoBuild, brick

    from brickwright import TestContext

    submodel = LegoBuild()
    submodel.add(brick(1, 1), name="leaf")
    model = LegoBuild()
    model.add(submodel, name="left")
    model.add(submodel, name="right")
    collisions = TestContext(model).check_no_collisions()
    (diagnostic,) = collisions.diagnostics
    (pair,) = diagnostic.model_dump(mode="json")["evidence"]["pairs"]
    assert diagnostic.code == "validation.collision"
    assert {tuple(part["path"]) for part in pair} == {
        ("left", "leaf"),
        ("right", "leaf"),
    }


def test_missing_connector_annotations_use_official_catalog_facts(
    record, config, in_process
):
    from bricklayer import lookup_part, parts_with_connectors

    snippet = (
        'bricklayer.Element("10040", bricklayer.Color.RED).find_connector("hinge_7_in")'
    )
    report = explore(record, snippet, config)
    cli = _external(record, config, "explore", "--code", snippet)
    assert cli["diagnostics"] == _as_cli(report.model_dump(mode="json")["diagnostics"])
    (issue,) = cli["diagnostics"]
    capabilities = lookup_part("10040")
    assert issue["stage"] == "explore"
    assert issue["code"] == "catalog.missing_data"
    assert issue["evidence"]["part_id"] == capabilities.part_id
    assert issue["evidence"]["version"] == capabilities.version
    assert issue["evidence"]["reason_code"] == capabilities.connectivity.reason_code
    assert issue["evidence"]["parts_with_connector"] == list(
        parts_with_connectors("hinge_7_in")[:5]
    )
    assert "lookup_part" in issue["next_action"]


def test_a_nonexistent_catalog_id_is_distinct_from_missing_annotations(
    record, config, in_process
):
    snippet = 'bricklayer.PartSpec("diagnostic-fixture-outside-release")'
    report = explore(record, snippet, config)
    cli = _external(record, config, "explore", "--code", snippet)
    assert cli["diagnostics"] == _as_cli(report.model_dump(mode="json")["diagnostics"])
    (issue,) = cli["diagnostics"]
    assert issue["stage"] == "explore"
    assert issue["code"] == "catalog.unknown_part"
    assert issue["evidence"]["part_id"] == "diagnostic-fixture-outside-release"
    assert issue["evidence"]["reason_code"] == "part_outside_version"
