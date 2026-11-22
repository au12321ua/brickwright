"""A build error can be repaired using the knowledge available to an agent."""

import json

from typer.testing import CliRunner

from brickwright.cli import app
from brickwright.tools.registry import execute_tool

_TWO_HOLES = """import bricklayer as bl

def create_build():
    model = bl.LegoBuild()
    beam = model.add(bl.Element("32000", bl.Color.RED), name="boom")
    pin = bl.Element("2780", bl.Color.BLACK)
    target = beam.find_connector("pin_hole")
    model.connect(pin, pin.find_connectors("pin")[0], to=target, name="pin")
    return model

def run_tests():
    from brickwright import TestContext
    checks = TestContext(create_build())
    checks.check_connected()
    return checks.report()
"""


def test_repair_an_ambiguous_port_using_the_packaged_guide(
    make_record, config, in_process, monkeypatch
):
    monkeypatch.setattr("brickwright.tools.rendering.render_available", lambda: False)
    record = make_record(_TWO_HOLES)
    failure = execute_tool(record, config, "validate", {})
    assert not failure.ok
    (issue,) = json.loads(failure.content)["diagnostics"]
    assert issue["code"] == "connector.ambiguous"
    assert issue["evidence"]["part_id"] == "32000"

    runner = CliRunner()
    found = runner.invoke(app, ["knowledge", "search", issue["code"]])
    assert found.exit_code == 0, found.output
    matches = json.loads(found.stdout)["matches"]
    guide_id = next(
        match["id"]
        for match in matches
        if issue["code"] in match["matched_on"].get("diagnostics", [])
    )
    guide = runner.invoke(app, ["knowledge", "show", guide_id])
    assert guide.exit_code == 0, guide.output
    entry = json.loads(guide.stdout)
    assert issue["code"] in entry["diagnostics"]
    assert "toward=" in entry["example"]["source"]

    repaired = _TWO_HOLES.replace(
        'beam.find_connector("pin_hole")',
        'beam.find_connector("pin_hole", toward=bl.Direction.LEFT)',
    )
    record.write_build(repaired)
    finalized = runner.invoke(
        app,
        ["external", "finalize", record.id, "--records-dir", str(config.records_dir)],
    )
    assert finalized.exit_code == 0, finalized.output
    assert record.read_meta().finalized
    assert record.ldr_path.is_file()
