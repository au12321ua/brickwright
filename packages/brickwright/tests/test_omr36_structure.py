import json
from hashlib import sha256

from bricklayer import Color, Element, LegoBuild, to_ldraw

from brickwright.evaluation.omr36_structure import Replay, audit_arm, audit_trial

TASK = "R-10258"
ATTEMPT = "tasks/R-10258/trial-1/attempt-1"
SOURCE = b"def create_build():\n    pass\n"


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(json.dumps(data))


def _example(root, *, pieces=2, modified_mpd=None, source=SOURCE):
    build = LegoBuild()
    build.add(Element("3001", Color.RED), (0, 0, 0), name="UNBLINDABLE studio model")
    build.add(Element("3001", Color.RED), (8, 0, 0))
    native_bytes = to_ldraw(build).encode()
    exported = modified_mpd if modified_mpd is not None else native_bytes
    product = root / ATTEMPT / "artifacts/logs/artifacts"
    _write(product / "model.mpd", exported)
    record = product / "records/version-identifying-label"
    _write(record / "build.py", source)
    _write(
        record / "meta.json",
        {"finalized": True, "exported_source_sha256": sha256(source).hexdigest()},
    )
    _write(record / "out/build.ldr", native_bytes)
    _write(
        record / "out/report.json",
        {
            "ok": True,
            "source_sha256": sha256(source).hexdigest(),
            "brick_count": 2,
            "checks": [
                {"name": "no_collisions", "passed": True},
                {"name": "no_floating", "passed": True},
            ],
        },
    )
    _write(
        record / "out/connectivity/components.json",
        {
            "component_count": 2,
            "components": [{"part_indices": [0]}, {"part_indices": [1]}],
        },
    )
    _write(
        root / ATTEMPT / "verifier/score-details.json",
        {
            "record_id": record.name,
            "pieces": pieces,
            "native_finalization": "pass",
            "delivery": "present",
            "fixed_and_inventory": "pass",
            "quality": {"views": {}},
        },
    )
    return {
        "task_id": TASK,
        "trial_index": 1,
        "attempt_path": ATTEMPT,
        "delivered": True,
        "model_sha256": sha256(exported).hexdigest(),
    }


def test_original_native_evidence_is_distinct_from_the_standard_blind_summary(tmp_path):
    row = _example(tmp_path)
    trial = audit_trial(tmp_path, row)
    assert trial["eligibility"] == "delivered"
    assert trial["native"]["archive"]["validation"]["brick_count"] == 2
    assert trial["native"]["archive"]["connectivity"]["component_sizes"] == [1, 1]
    assert trial["native"]["replay"] is None
    assert trial["standard"]["column"]["components"][1]["members"] == [1]
    assert trial["blind"]["component_count"] == 2
    assert trial["blind"]["largest_component_part_count"] == 1
    blind = json.dumps(trial["blind"])
    for private in (
        "baseline",
        "candidate",
        ATTEMPT,
        "sdk",
        "record",
        "version-identifying-label",
        row["model_sha256"],
    ):
        assert private not in blind


def test_skipped_and_missing_export_evidence_is_retained(tmp_path):
    row = _example(tmp_path, modified_mpd=b"1 4 0 0 0 1 0 0 0 1 0 0 0 1 unknown1.dat\n")
    trial = audit_trial(tmp_path, row)
    assert trial["blind"]["analysis"] == "incomplete"
    assert trial["standard"]["column"]["warnings"]
    assert trial["native"]["archive"] is None

    row["task_id"] = "R-21011"
    row.pop("attempt_path")
    row["delivered"] = None
    trial = audit_trial(tmp_path, row)
    assert trial["blind"]["analysis"] == "unavailable"
    assert trial["eligibility"] == "unknown"


def test_arm_keeps_uncollected_trial_and_rejected_submission(tmp_path):
    archive = tmp_path / "run"
    row = _example(archive)
    row["delivered"] = False
    _write(archive / "index.json", {"trials": [row]})
    output = tmp_path / "private"
    index = audit_arm(archive, output, tasks=(TASK,), repetitions=2)
    assert index["coverage"] == {
        "expected": 2,
        "delivered": 0,
        "not_delivered": 1,
        "unknown": 1,
        "mpds_checked": 1,
        "complete_deliveries": 0,
    }
    trial = json.loads((output / TASK / "trial-1.json").read_text())
    assert trial["standard"]["column"]["part_count"] == 2
    assert trial["blind"]["analysis"] == "unavailable"
    assert (output / TASK / "trial-2.json").is_file()


def test_controlled_replay_preserves_local_endpoints_without_contaminating_standard(
    tmp_path,
):
    source = b"""
from bricklayer import LegoBuild, Element, Color

class NoEndpoints(Element):
    @property
    def connectors(self):
        return frozenset()

def create_build():
    model = LegoBuild()
    model.add(NoEndpoints("3001", Color.RED), (0, 0, 0))
    model.add(Element("3001", Color.RED), (8, 0, 0))
    return model
"""
    row = _example(tmp_path, source=source)
    result = audit_trial(tmp_path, row, replay=Replay(revision="fixture"))
    evidence = result["native"]["replay"]
    assert evidence["exact_delivery_bytes"]
    assert evidence["matches_archived_native_components"]
    (override,) = evidence["endpoint_overrides"]
    assert override["part_id"] == "3001"
    assert override["instances"] == [0]
    assert override["record_local"] == []
    assert override["original_sdk_default"]
    assert result["standard"] == audit_trial(tmp_path, row)["standard"]
