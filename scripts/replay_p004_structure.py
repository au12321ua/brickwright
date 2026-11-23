"""Reproduce offline P-004 evidence using the frozen OMR36 v2 inputs."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bricklayer import LegoBuild, Placement, lookup_part
from bricklayer.io import read_ldraw
from bricklayer.parts import Element
from brickwright.config import Config
from brickwright.flow import flow_status, set_artifact
from brickwright.flow.artifacts import Plan, read_conditions
from brickwright.flow.structure import StructureReport, classify_structure, inspect_mpd
from brickwright.record import Record
from brickwright.tools.build_execution import load_build_module, run_in_subprocess
from brickwright.tools.finalization import finalize
from brickwright.tools.validation import validate

TASKS = {
    "R-10258": (1684, 14),
    "R-21011": (362, 1),
    "R-21020": (730, 1),
    "R-31038": (545, 19),
    "R-31039": (613, 1),
    "R-5521": (395, 4),
}
HOUSE = "R-31038"
BUS = "R-10258"
FIXTURE_REVIEW = {"current": True, "passed": True, "rounds": 1, "remaining_items": []}
Leaf = Placement[Element]


def write_json(path: Path, report: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


def codes(result: dict) -> set[str]:
    return {diagnostic["code"] for diagnostic in result["diagnostics"]}


def selector(part: Leaf) -> dict:
    return {
        "part_id": part.content.ldraw_id,
        "position": tuple(float(value) for value in part.pose.translation),
        "rotation": part.pose.rotation.entries,
    }


def features_for(task: dict) -> dict:
    features = [
        {
            "id": f"F{i:02}",
            "name": text,
            "description": text,
            "category": "retain",
            "importance": "required",
            "evidence": [{"check_id": f"C{i:02}"}],
            "rationale": "Keep the exact task condition in the acceptance replay.",
        }
        for i, text in enumerate(task["checks"], 1)
    ]
    if task["id"] == HOUSE:
        features.append(
            {
                "id": "window-leaf",
                "name": "Decorative panes",
                "description": "Leave each pane seated when the catalog omits its hinge.",
                "category": "simplify",
                "importance": "preferred",
                "evidence": [
                    {
                        "image": "goal-angle.png",
                        "view": "angle",
                        "region": "cottage windows",
                    }
                ],
                "rationale": "The window opening remains required.",
                "simplification": "Permit seated decorative panes at the verified hinge positions.",
            }
        )
    return {"schema_version": 1, "features": features}


def verify_conditions(record: Record, task_path: Path, features: dict) -> dict:
    task_bytes = task_path.read_bytes()
    task = json.loads(task_bytes)
    conditions = read_conditions(record)
    assert record.path.joinpath("task.json").read_bytes() == task_bytes
    assert conditions.task_id == task["id"]
    assert conditions.task_sha256 == sha256(task_bytes).hexdigest()
    assert [check.id for check in conditions.checks] == ["C01", "C02", "C03"]
    assert [check.text for check in conditions.checks] == task["checks"]
    missing = deepcopy(features)
    del missing["features"][1]
    assert "flow.features" in codes(set_artifact(record, "features", missing))
    assert not codes(set_artifact(record, "features", features))
    protected = record.path / "conditions.json"
    original = protected.read_bytes()
    for variant in ("delete", "replace"):
        changed = json.loads(original)
        if variant == "delete":
            del changed["checks"][1]
        else:
            changed["checks"][1]["text"] += " changed"
        write_json(protected, changed)
        assert "flow.conditions" in codes(flow_status(record))
        protected.write_bytes(original)
    assert "flow.conditions" not in codes(flow_status(record))
    return conditions.model_dump(mode="json")


def plan_for(task_id: str, model: LegoBuild, *, archived: bool) -> dict:
    plan: dict[str, Any] = {
        "schema_version": 1,
        "bounds": {
            "minimum": [-1000, -1000, -1500],
            "maximum": [1000, 500, 1000],
        },
        "colors": sorted(
            {part.content.color.display_name for part in model.flattened_placements}
        ),
        "independent_objects": [],
        "decorative_interfaces": [],
    }
    if task_id == HOUSE:
        roadster = (
            model.names["roadster"].content.flattened_placements
            if archived
            else next(group for group in model.connected_components if len(group) == 47)
        )
        assert len(roadster) == (31 if archived else 47)
        plan["independent_objects"].append(
            {
                "id": "roadster",
                "purpose": "Roadster parked independently in the target scene",
                "members": [selector(part) for part in roadster],
            }
        )
        host_id, child_id, gap = "60594", "60608", "missing_endpoint"
        reason, feature_name = (
            "Window leaf seated in the matched frame recess",
            "window-leaf",
        )
    elif task_id == BUS:
        host_id, child_id, gap = "56904", "30699", "endpoint_offset"
        reason, feature_name = "Tyre and rim must connect as a functional wheel", None
    else:
        return plan

    leaves = model.flattened_placements
    hosts = [part for part in leaves if part.content.ldraw_id == host_id]
    children = [part for part in leaves if part.content.ldraw_id == child_id]
    for i, child in enumerate(children, 1):
        host = min(
            hosts,
            key=lambda part: (
                (part.pose.translation - child.pose.translation).to_stud_units().norm()
            ),
        )
        plan["decorative_interfaces"].append(
            {
                "id": f"{child_id}-{i:02}",
                "host": selector(host),
                "child": selector(child),
                "required": task_id == BUS,
                "gap": gap,
                "source": "P-001",
                "reason": reason,
                "feature_id": feature_name,
            }
        )
    return plan


def saved_structure(
    record: Record, config: Config, plan: dict
) -> tuple[dict, StructureReport]:
    outcome = set_artifact(record, "plan", plan)
    assert not codes(outcome), outcome
    validation = validate(record, config)
    state = flow_status(record)
    assert state["snapshot_id"], validation
    assert state["mpd_path"], validation
    mpd_path = Path(state["mpd_path"])
    report = StructureReport.model_validate_json(
        mpd_path.with_name("structure.json").read_bytes()
    )
    return state, report


def summarize(report: StructureReport) -> dict:
    values = {}
    for name, column in (("native", report.native), ("standard", report.standard)):
        assert column is not None
        values[name] = {
            "parts": column.part_count,
            "components": sorted(
                (component.part_count for component in column.components),
                reverse=True,
            ),
            "collisions": len(column.collisions),
            "floating": len(column.floating),
            "contact_only": len(column.contact_only_parts),
            "classifications": dict(
                Counter(
                    item["kind"]
                    for item in report.classifications
                    if item["column"] == name
                )
            ),
            "import_warnings": column.warnings,
        }
    return values | {
        "passed": report.passed,
        "complete": report.complete,
        "mpd_sha256": report.mpd_sha256,
        "limitations": [item["interface_id"] for item in report.limitations],
        "diagnostics": sorted({diagnostic.code for diagnostic in report.diagnostics}),
    }


def save_case(
    output: Path, name: str, record: Record, state: dict, report: StructureReport
) -> dict:
    write_json(output / name / "structure.json", report.model_dump(mode="json"))
    write_json(output / name / "status.json", state)
    (output / name / "plan.json").write_bytes((record.path / "plan.json").read_bytes())
    return summarize(report) | {
        "record": record.id,
        "snapshot_id": state["snapshot_id"],
    }


def replay_reference(task_dir: Path, config: Config, output: Path) -> tuple[dict, dict]:
    task_path = task_dir / "environment/input/task.json"
    task = json.loads(task_path.read_bytes())
    task_id = task["id"]
    source = task_dir / "solution/reference.mpd"
    source_bytes = source.read_bytes()
    source_copy = output / "reference" / task_id / "original.mpd"
    source_copy.parent.mkdir(parents=True, exist_ok=True)
    source_copy.write_bytes(source_bytes)
    record = Record.create(
        config, prompt=f"Reference replay {task_id}", task_path=task_path
    )
    conditions = verify_conditions(record, task_path, features_for(task))
    record.write_build(
        "from bricklayer.io import read_ldraw\n\n"
        f"def create_build():\n    return read_ldraw({str(source_copy)!r})\n"
        "\ndef run_tests():\n    return []\n"
    )
    plan = plan_for(task_id, read_ldraw(source), archived=False)
    state, product = saved_structure(record, config, plan)
    name = f"reference/{task_id}"
    summary = save_case(output, name, record, state, product)

    # The reference author MPD contains comments and precision not emitted by the SDK.
    # Also check those exact archive bytes against the same record-native column.
    standard, diagnostics = run_in_subprocess(
        inspect_mpd, (source_bytes, Plan.model_validate(plan)), config.build_timeout
    )
    report = classify_structure(
        product.native, standard, source_bytes, Plan.model_validate(plan), diagnostics
    )
    write_json(
        output / name / "original-structure.json", report.model_dump(mode="json")
    )
    assert report.complete and product.complete, (summary, summarize(report))
    assert report.native is not None
    parts, components = TASKS[task_id]
    for column in (report.native, standard):
        assert column.part_count == parts
        assert len(column.components) == components
        assert not column.collisions and not column.floating
    expected = {
        BUS: {"unknown": 13},
        HOUSE: {"intentional": 1, "data_gap": 6, "unknown": 11},
        "R-5521": {"unknown": 3},
    }.get(task_id, {})
    for column_name in ("native", "standard"):
        actual = Counter(
            item["kind"]
            for item in report.classifications
            if item["column"] == column_name
        )
        assert actual == expected, (task_id, column_name, actual)
    if task_id == BUS:
        assert len(standard.interface_evidence) == 4
        for pair in standard.interface_evidence.values():
            assert not pair["mated"] and pair["direct_contact"]
            assert pair["candidate_count"] == 32
            assert all(
                trial["diagnostic_codes"] == ["assembly.collision"]
                for trial in pair["trials"]
            )
    assert report.passed is product.passed is (components == 1)
    return summary | {"original": summarize(report)}, conditions


def replay_archive(
    baseline: Path, task_dir: Path, config: Config, output: Path
) -> tuple[Record, dict, dict, StructureReport]:
    task_id = task_dir.name
    task_path = task_dir / "environment/input/task.json"
    archived = (
        baseline / "tasks" / task_id / "trial-1/attempt-1/artifacts/logs/artifacts"
    )
    (original_source,) = sorted((archived / "records").glob("*/build.py"))
    original_mpd = (archived / "model.mpd").read_bytes()
    name = f"archive/{task_id}"
    (output / name).mkdir(parents=True, exist_ok=True)
    (output / name / "original.mpd").write_bytes(original_mpd)
    (output / name / "original-build.py").write_bytes(original_source.read_bytes())

    record = Record.create(
        config, prompt=f"Archive replay {task_id}", task_path=task_path
    )
    features = features_for(json.loads(task_path.read_bytes()))
    assert not codes(set_artifact(record, "features", features))
    replay_source = original_source.read_text().replace(
        "/app/input", task_path.parent.as_posix()
    )
    record.write_build(replay_source)
    native_model = load_build_module(record.build_path, replay_source).create_build()
    plan = plan_for(task_id, native_model, archived=True)
    state, report = saved_structure(record, config, plan)
    summary: dict[str, Any] = save_case(output, name, record, state, report) | {
        "original_record": original_source.parent.name,
        "original_source_sha256": sha256(original_source.read_bytes()).hexdigest(),
        "replayed_source_sha256": record.source_sha256,
    }
    assert report.complete, summary
    assert Path(state["mpd_path"]).read_bytes() == original_mpd, summary
    assert report.native is not None and report.standard is not None
    assert summary["native"]["components"] == (
        [507, 31, 1, 1, 1, 1, 1, 1] if task_id == HOUSE else [1007]
    ), summary
    assert summary["standard"]["components"] == (
        [507, 31, 1, 1, 1, 1, 1, 1] if task_id == HOUSE else [1003, 1, 1, 1, 1]
    ), summary
    assert not (report.native.collisions or report.standard.collisions)
    assert not (report.native.floating or report.standard.floating)
    assert state["structural_passed"] is report.passed is (task_id == HOUSE), summary
    assert "flow.review" in codes(state)
    interfaces = plan["decorative_interfaces"]
    assert len(interfaces) == (6 if task_id == HOUSE else 4)
    for interface in interfaces:
        key = interface["id"]
        native = report.native.interface_evidence[key]
        standard = report.standard.interface_evidence[key]
        assert native["mated"] is (task_id == BUS)
        assert (
            not standard["mated"]
            and standard["direct_contact"]
            and standard["search_complete"]
        )
        assert standard["candidate_count"] == (0 if task_id == HOUSE else 32)
        assert all(
            trial["diagnostic_codes"] == ["assembly.collision"]
            for trial in standard["trials"]
        )
    return record, plan, summary, report


def synthesize_house_source(source: str, effect: str) -> str:
    original = source.split("def create_build():")[1].split("def run_tests():")[0]
    assert "return scene.build" in original
    common = """
    build = scene.build
    pane = next(p for p in build.placements
                if isinstance(p.content, bl.Element) and p.content.ldraw_id == "60608")
"""
    additions = {
        "different_pose": """
    build.remove(pane)
    shift = bl.Position(0.0005, 0, 0)
    build.add_raw(pane.content, bl.Pose(pane.pose.rotation, pane.pose.translation + shift))
""",
        "moved": """
    floor = min(p.bounding_box[0].z for p in build.flattened_placements)
    build.remove(pane)
    shift = bl.Position(50, 0, floor - pane.bounding_box[0].z)
    build.add_raw(pane.content, bl.Pose(pane.pose.rotation, pane.pose.translation + shift))
""",
        "unrelated_no_data": """
    floor = min(p.bounding_box[0].z for p in build.flattened_placements)
    spare = bl.Element("10040", bl.Color.WHITE)
    build.add_raw(spare, bl.Pose(translation=bl.Position(
        50, 0, floor - spare.bounding_box[0].z)))
    build.add_raw(bl.Element("3001", bl.Color.RED),
                  bl.Pose(translation=bl.Position(54, 0, floor)))
""",
        "collisions": "    build.add_raw(pane.content, pane.pose)\n",
        "floating": """
    build.add_raw(bl.Element("3001", bl.Color.RED),
                  bl.Pose(translation=bl.Position(50, 0, 30)))
""",
    }
    synthetic = original.replace(
        "    return scene.build", common + additions[effect] + "    return build"
    )
    return (
        source.replace(original, synthetic).split("def run_tests():")[0]
        + "def run_tests():\n    return []\n"
    )


def replay_boundary(
    record: Record,
    plan: dict,
    report: StructureReport,
    config: Config,
    output: Path,
) -> dict:
    original_source = record.read_build()
    assert report.native is not None and report.standard is not None
    prepared = Plan.model_validate(plan)
    raw_mpd = Path(flow_status(record)["mpd_path"]).read_bytes()
    assert len(report.limitations) == 6 and report.passed
    controls = {}
    editable = ("required", "no_evidence", "different_pair")
    effects = ("different_pose", "moved", "unrelated_no_data", "collisions", "floating")
    for name in (*editable, *effects):
        altered = deepcopy(plan)
        record.write_build(
            synthesize_house_source(original_source, name)
            if name in effects
            else original_source
        )
        first = altered["decorative_interfaces"][0]
        if name == "required":
            first["required"] = True
        elif name == "no_evidence":
            del first["gap"]
            del first["source"]
        elif name == "different_pair":
            first["host"] = deepcopy(altered["independent_objects"][0]["members"][0])
        elif name == "different_pose":
            x, y, z = first["child"]["position"]
            first["child"]["position"] = (x + 0.0005, y, z)
        state, result = saved_structure(record, config, altered)
        assert not (state["structural_passed"] or result.passed)
        assert result.native is not None and result.standard is not None
        if name in ("collisions", "floating"):
            assert getattr(result.native, name) and getattr(result.standard, name)
        else:
            assert not any(
                (
                    result.native.collisions,
                    result.standard.collisions,
                    result.native.floating,
                    result.standard.floating,
                )
            ), summarize(result)
        controls[name] = save_case(output, f"boundary/{name}", record, state, result)
        if name == "different_pose":
            for column in (result.native, result.standard):
                local = column.interface_evidence[first["id"]]
                assert local["direct_contact"] and not local["mated"], local
        if name in ("no_evidence", "collisions", "floating"):
            finalized = finalize_with_fixture_review(record, config)
            assert not finalized.ok and not record.ldr_path.is_file()
            assert any(
                diagnostic.code == "flow.structure"
                for diagnostic in finalized.diagnostics
            )
            controls[name]["finalize_blocked"] = True

    # Isolate each column's participation in the gate. These flags are synthetic
    # in this consumer test. Real collisions and floating parts are tested above.
    for column_name, physical_issue in (
        ("native", "collisions"),
        ("standard", "floating"),
    ):
        columns = {"native": report.native, "standard": report.standard}
        entries = [(0, 1)] if physical_issue == "collisions" else [0]
        columns[column_name] = columns[column_name].model_copy(
            update={physical_issue: entries}
        )
        result = classify_structure(
            columns["native"], columns["standard"], raw_mpd, prepared
        )
        assert not result.passed
        name = f"only_{column_name}_{physical_issue}"
        write_json(
            output / "boundary" / name / "structure.json",
            result.model_dump(mode="json"),
        )
        controls[name] = summarize(result)

    # Use the exact native geometry and independently perturb the reread bytes.
    old_lines = raw_mpd.splitlines(keepends=True)
    omitted = next(
        i for i, line in enumerate(old_lines) if line.strip().endswith(b"60608.dat")
    )
    for name, changed in (
        (
            "unsupported_standard_geometry",
            old_lines[:3]
            + [b"1 4 0 0 0 1 0 0 0 1 0 0 0 1 unknown-p004.dat\n"]
            + old_lines[3:],
        ),
        ("missing_standard_part", old_lines[:omitted] + old_lines[omitted + 1 :]),
    ):
        incomplete = b"".join(changed)
        standard, diagnostics = run_in_subprocess(
            inspect_mpd, (incomplete, prepared), config.build_timeout
        )
        result = classify_structure(
            report.native, standard, incomplete, prepared, diagnostics
        )
        assert not result.complete and not result.passed
        assert (
            standard.warnings
            if name == "unsupported_standard_geometry"
            else standard.part_count == 543
        )
        write_json(
            output / "boundary" / name / "structure.json",
            result.model_dump(mode="json"),
        )
        (output / "boundary" / name / "changed.mpd").write_bytes(incomplete)
        controls[name] = summarize(result)
    record.write_build(original_source)
    saved_structure(record, config, plan)
    return controls


def finalize_with_fixture_review(record: Record, config: Config):
    # This exercises publication without claiming that an independent reviewer ran.
    with (
        patch("brickwright.flow.state.review_status", return_value=FIXTURE_REVIEW),
        patch.dict(os.environ, LDRAW_LIBRARY_PATH=""),
    ):
        return finalize(record, config)


def replay_finalize(record: Record, plan: dict, config: Config, output: Path) -> dict:
    outcomes = {}
    for name, required in (("permitted", False), ("required", True)):
        changed = deepcopy(plan)
        changed["decorative_interfaces"][0]["required"] = required
        state, report = saved_structure(record, config, changed)
        result = finalize_with_fixture_review(record, config)
        assert result.ok == (not required), result
        assert record.ldr_path.is_file() == (not required)
        if not required:
            assert sha256(record.ldr_path.read_bytes()).hexdigest() == report.mpd_sha256
            assert result.flow and len(result.flow["limitations"]) == 6
            (output / "boundary" / "fixture-delivered.mpd").write_bytes(
                record.ldr_path.read_bytes()
            )
        outcomes[name] = {
            "fixture_review": FIXTURE_REVIEW,
            "structure": summarize(report),
            "status_before_fixture": state,
            "finalize": result.model_dump(mode="json"),
        }
    write_json(output / "boundary" / "fixture-finalize.json", outcomes)
    return {name: outcome["finalize"]["ok"] for name, outcome in outcomes.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--ldraw-library", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    tasks = args.dataset.resolve(strict=True) / "harbor-tasks"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ["LDRAW_LIBRARY_PATH"] = str(args.ldraw_library.resolve(strict=True))
    config = Config(records_dir=output / "records", flow=True, build_timeout=90)
    assert (
        lookup_part("10040").connectivity.reason_code == "missing_connector_annotations"
    )
    results: dict[str, Any] = {
        "conditions": {},
        "reference": {},
        "archive": {},
        "boundary": {},
    }
    for task_id in TASKS:
        summary, conditions = replay_reference(tasks / task_id, config, output)
        results["conditions"][task_id] = conditions
        results["reference"][task_id] = summary
        print(f"reference {task_id} complete", flush=True)
    assert sum(len(task["checks"]) for task in results["conditions"].values()) == 18
    for task_id in (HOUSE, BUS):
        record, plan, summary, report = replay_archive(
            args.baseline.resolve(strict=True), tasks / task_id, config, output
        )
        results["archive"][task_id] = summary
        print(f"archive {task_id} complete", flush=True)
        if task_id == HOUSE:
            results["boundary"] = replay_boundary(record, plan, report, config, output)
            results["fixture_finalize"] = replay_finalize(record, plan, config, output)
    write_json(output / "summary.json", results)
    print(output / "summary.json")


if __name__ == "__main__":
    main()
