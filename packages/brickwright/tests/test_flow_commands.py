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
    created = json.loads(result.stdout)
    record_id = created["record_id"]
    draft = records / record_id / "drafts/features.json"
    assert created["next"]["kind"] == "edit"
    assert created["next"]["path"] == str(draft.resolve())
    assert [item["id"] for item in json.loads(draft.read_text())["features"]] == ["C01"]
    config = Config(records_dir=records, flow=True)
    record = Record.load(config, record_id)
    assert record.read_meta().flow
    features = runner.invoke(app, ["external", "features", record_id])
    assert features.exit_code == 0, features.output
    shown = json.loads(features.stdout)
    assert shown.pop("ok") is True and shown.pop("next") is None
    assert shown == read_artifact(record, "features")
    status = runner.invoke(app, ["external", "status", record_id])
    assert status.exit_code == 0, status.output
    assert not json.loads(status.stdout)["ready"]
    invalid = tmp_path / "bad.json"
    invalid.write_text("{")
    result = runner.invoke(app, ["external", "plan", record_id, "--set", str(invalid)])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert "JSON" in payload["error"]
    assert payload["diagnostics"][0]["code"] == "flow.plan"
    assert payload["next"]["kind"] == "edit"
    assert payload["next"]["path"] == str(invalid.absolute())
    assert payload["next"]["then"]["argv"][-4:] == [
        "--set",
        str(invalid.absolute()),
        "--records-dir",
        str(records.absolute()),
    ]


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
