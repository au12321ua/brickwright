"""Flow commands and the workflow API exercise the same stored artifacts."""

import json
from pathlib import Path

from typer.testing import CliRunner

from brickwright.cli import app
from brickwright.config import Config
from brickwright.flow import read_artifact
from brickwright.record import Record


def test_external_flow_import_and_workflow_read(tmp_path: Path, monkeypatch):
    records = tmp_path / "records"
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"id": "R-1", "checks": ["A red deck"]}))
    monkeypatch.setenv("BRICKWRIGHT_RECORDS_DIR", str(records))
    monkeypatch.setenv("BRICKWRIGHT_TASK_JSON", str(task))
    runner = CliRunner()
    result = runner.invoke(app, ["external", "init", "a boat", "--flow"])
    assert result.exit_code == 0, result.output
    record_id = dict(line.split("=", 1) for line in result.stdout.splitlines())[
        "record_id"
    ]
    config = Config(records_dir=records, flow=True)
    record = Record.load(config, record_id)
    assert record.read_meta().flow
    features = runner.invoke(app, ["external", "features", record_id])
    assert features.exit_code == 0, features.output
    assert json.loads(features.stdout) == read_artifact(record, "features")
    status = runner.invoke(app, ["external", "status", record_id])
    assert status.exit_code == 0, status.output
    assert not json.loads(status.stdout)["ready"]
    invalid = tmp_path / "bad.json"
    invalid.write_text("{")
    result = runner.invoke(app, ["external", "plan", record_id, "--set", str(invalid)])
    assert result.exit_code != 0
    assert result.exception is not None
    assert "JSON" in result.output


def test_flow_preview_does_not_publish_a_formal_ldraw(
    tmp_path, valid_build, fake_render_pipeline, in_process
):
    from brickwright.tools.rendering import render

    config = Config(records_dir=tmp_path, flow=True)
    record = Record.create(config, prompt="two bricks")
    record.write_build(valid_build)
    assert render(record, config).ok
    assert record.render_path.is_file()
    assert not record.ldr_path.exists()
