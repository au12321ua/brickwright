"""The facts available to internal and external agents after execution failures."""

import json
from hashlib import sha256

import pytest
from typer.testing import CliRunner

from brickwright.cli import app
from brickwright.tools import rendering
from brickwright.tools.registry import execute_tool
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
    tool = execute_tool(record, config, "validate", {})
    payload = json.loads(tool.content)
    cli = _external(record, config, "validate")
    assert cli["diagnostics"] == payload["diagnostics"]
    (issue,) = cli["diagnostics"]
    assert (issue["stage"], issue["code"]) == (stage, code)
    assert issue["evidence"]["source"]["file"] == "build.py"
    assert issue["evidence"]["source"]["line"] == line
    assert issue["evidence"]["source"]["text"] == source.splitlines()[line - 1]
    assert issue["evidence"]["exception_type"] == cli["error_type"]
    assert issue["evidence"]["exception_message"]
    assert cli["checks"] == []
    archived = config.records_dir / record.id / cli["report_path"]
    assert (
        sha256(archived.with_name("build.py").read_bytes()).hexdigest()
        == cli["source_sha256"]
    )
    assert json.loads(archived.read_text()) == cli


@pytest.mark.parametrize(
    "name,arguments,code,parameter",
    [
        ("write_build", {}, "tool.argument_missing", "content"),
        (
            "render",
            {"azimuth": 0, "elevation": 100},
            "tool.argument_out_of_range",
            "elevation",
        ),
    ],
)
def test_parameter_diagnostics_show_the_failed_field(
    record, config, name, arguments, code, parameter
):
    result = execute_tool(record, config, name, arguments)
    (issue,) = json.loads(result.content)["diagnostics"]
    assert issue["code"] == code
    assert issue["stage"] == "tool_call"
    assert issue["evidence"]["parameter"] == parameter


def test_atomic_edit_conflict_keeps_the_nearest_match(record, config):
    record.write_build("first_line = 1\nsecond_line = 2\n")
    result = execute_tool(
        record,
        config,
        "multi_edit_build",
        {
            "edits": [
                {"old_str": "first_line = 1", "new_str": "first_line = 3"},
                {"old_str": "second_line = 4", "new_str": "second_line = 5"},
            ],
        },
    )
    (issue,) = json.loads(result.content)["diagnostics"]
    assert issue["code"] == "edit.no_match"
    assert issue["stage"] == "edit"
    assert issue["evidence"]["edit_index"] == 2
    assert issue["evidence"]["applied_edits"] == 0
    assert issue["evidence"]["closest_match"]["start_line"] == 2
    assert "second_line = 2" in issue["evidence"]["closest_match"]["snippet"]
    assert record.read_build() == "first_line = 1\nsecond_line = 2\n"


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
    tool = execute_tool(record, config, "explore", {"code": snippet})
    cli = _external(record, config, "explore", "--code", snippet)
    assert cli == json.loads(tool.content)
    (issue,) = cli["diagnostics"]
    assert issue["evidence"]["source"]["file"] == file
    assert issue["evidence"]["source"]["line"] == line
    assert issue["stage"] == stage


def test_connector_ambiguity_carries_actual_candidates(make_record, config, in_process):
    record = make_record("""import bricklayer as bl
def create_build():
    return bl.Element("3001", bl.Color.RED).find_connector("stud")
""")
    tool = execute_tool(record, config, "validate", {})
    (issue,) = json.loads(tool.content)["diagnostics"]
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
    tool = execute_tool(record, config, "finalize", {})
    cli = _external(record, config, "finalize")
    assert not tool.done
    assert not record.ldr_path.exists()
    issues = cli["validation"]["diagnostics"]
    assert issues == json.loads(tool.content)["validation"]["diagnostics"]
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
    tool = execute_tool(record, config, "finalize", {})
    cli = _external(record, config, "finalize")
    assert cli["validation"]["ok"] and not cli["ok"]
    (issue,) = cli["diagnostics"]
    assert (issue["stage"], issue["code"]) == ("export", "output.export_failed")
    assert issue["evidence"]["exception_message"] == "export fixture failed"
    assert issue["evidence"]["path"] == "out/build.ldr"
    assert json.loads(tool.content)["diagnostics"] == cli["diagnostics"]
    assert not record.ldr_path.exists()


@pytest.mark.integration
def test_timeout_and_abnormal_exit_include_worker_facts(
    make_record, config, infinite_build
):
    timeout_config = config.model_copy(update={"build_timeout": 0.2})
    timeout = execute_tool(make_record(infinite_build), timeout_config, "validate", {})
    (issue,) = json.loads(timeout.content)["diagnostics"]
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
    failed = execute_tool(record, config, "finalize", {})
    (issue,) = failed.diagnostics
    assert issue.code == "connector.ambiguous"
    assert issue.evidence["matches"] == 2
    assert not record.ldr_path.exists()
    old_report = record.snapshots[0].report_path.read_text()

    edit = execute_tool(
        record,
        config,
        "edit_build",
        {
            "old_str": 'base.find_connector("stud")',
            "new_str": 'base.find_connector("stud", toward=bl.Position(0.5, 0.5, 1))',
        },
    )
    assert edit.ok
    monkeypatch.setattr(rendering, "render_available", lambda: False)
    corrected = execute_tool(record, config, "finalize", {})
    assert corrected.ok and corrected.done
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
    result = execute_tool(record, config, "validate", {})
    payload = json.loads(result.content)
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
    tool = execute_tool(record, config, "render", {})
    cli = _external(record, config, "render")
    (issue,) = cli["diagnostics"]
    assert (issue["stage"], issue["code"]) == ("render", "output.ldraw_library_missing")
    assert issue["evidence"]["environment_variable"] == "LDRAW_LIBRARY_PATH"
    assert "LDRAW_LIBRARY_PATH" in issue["next_action"]
    assert json.loads(tool.content)["diagnostics"] == cli["diagnostics"]


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
    tool = execute_tool(record, config, "explore", {"code": snippet})
    cli = _external(record, config, "explore", "--code", snippet)
    assert cli["diagnostics"] == json.loads(tool.content)["diagnostics"]
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
    tool = execute_tool(record, config, "explore", {"code": snippet})
    cli = _external(record, config, "explore", "--code", snippet)
    assert cli["diagnostics"] == json.loads(tool.content)["diagnostics"]
    (issue,) = cli["diagnostics"]
    assert issue["stage"] == "explore"
    assert issue["code"] == "catalog.unknown_part"
    assert issue["evidence"]["part_id"] == "diagnostic-fixture-outside-release"
    assert issue["evidence"]["reason_code"] == "part_outside_version"
