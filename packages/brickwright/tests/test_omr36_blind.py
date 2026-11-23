import json

import pytest
from PIL import Image

from brickwright.evaluation import omr36_blind as blind


def dump(path, data):
    path.write_text(json.dumps(data))


def png(path):
    Image.new("RGBA", (12, 16), (2, 3, 4, 255)).save(path)


def make_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(blind, "TASKS", ("R-example",))
    inputs = tmp_path / "input" / "R-example" / "environment" / "input"
    inputs.mkdir(parents=True)
    dump(
        inputs / "task.json",
        {
            "id": "R-example",
            "checks": ["one", "two", "three"],
            "views": list(blind.VIEWS),
        },
    )
    for view in blind.VIEWS:
        png(inputs / f"goal-{view}.png")
    for arm in ("baseline", "candidate"):
        for trial in (1, 2):
            report = (
                tmp_path
                / arm
                / "tasks"
                / "R-example"
                / f"trial-{trial}"
                / "attempt-1"
                / "verifier"
            )
            report.mkdir(parents=True)
            for view in blind.VIEWS:
                png(report / f"submission-{view}.png")
            dump(
                report / "score-details.json",
                {
                    "delivery": "present",
                    "fixed_and_inventory": "pass",
                    "native_finalization": "pass",
                    "score": 0.5,
                    "quality": {
                        "views": {
                            v: {"silhouette_iou": 0.8, "rgb_agreement_over_union": 0.5}
                            for v in blind.VIEWS
                        }
                    },
                },
            )
    return (
        tmp_path / "input",
        tmp_path / "baseline",
        tmp_path / "candidate",
        tmp_path / "packet",
    )


def test_publish_blind_and_score_all_trials(tmp_path, monkeypatch):
    inputs, old, new, output = make_inputs(tmp_path, monkeypatch)
    blind.prepare(inputs, old, new, output)
    rubric = json.loads((output / "review" / "scores.json").read_text())
    assert len(rubric) == 4
    assert len({k for k in rubric}) == 4
    for ident, scores in rubric.items():
        assert (output / "review" / ident / "model-top.png").exists()
        for item in scores:
            assert item["result"] is None
            item.update(result="met", evidence="top, bow center")
    assert not any(
        x in (output / "review" / "index.html").read_text()
        for x in ("baseline", "candidate", "trial-")
    )
    dump(output / "review" / "scores.json", rubric)
    result = blind.score(output)
    assert result["status"] == "pass"
    assert result["arms"]["candidate"]["conditions_met"] == 6
    next((output / "review").glob("*/model-top.png")).write_bytes(b"edited")
    with pytest.raises(ValueError, match="changed"):
        blind.score(output)


def test_missing_trial_does_not_enter_blinded_packet(tmp_path, monkeypatch):
    inputs, old, new, output = make_inputs(tmp_path, monkeypatch)
    next(new.rglob("score-details.json")).unlink()
    with pytest.raises(ValueError, match="score"):
        blind.prepare(inputs, old, new, output)
    assert not output.exists()
