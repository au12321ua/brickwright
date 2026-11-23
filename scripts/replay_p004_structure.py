"""Reproduce offline P-004 evidence using the frozen OMR36 v2 inputs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from hashlib import file_digest, sha256
from importlib.metadata import version
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bricklayer
from bricklayer import LegoBuild, Placement, lookup_part
from bricklayer.io import read_ldraw
from bricklayer.parts import Element
from bricklayer.parts.official import OFFICIAL_PARTS_VERSION
from brickwright.config import Config
from brickwright.flow import flow_status, set_artifact
from brickwright.flow.artifacts import Plan, read_conditions
from brickwright.flow.structure import (
    Column,
    StructureReport,
    classify_structure,
    inspect_mpd,
)
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


CATALOG_PROFILES = {
    "historical-pre-p007": "c091fcce1e79bde06b59349146e3c40346d72b0d890ee05650ccc2c56d2b05a7",
    "integrated-p007": "3060827da8b32194dae89907a368437316257f7c6c9d83a1b7ac9a438132e8b7",
}
BUS_DISCONNECTED = [1003, 1, 1, 1, 1]
HOUSE_COMPONENTS = [507, 31, 1, 1, 1, 1, 1, 1]


@dataclass(frozen=True)
class ReplayOutput:
    path: Path
    sdk: dict[str, Any]

    @property
    def repaired_wheels(self) -> bool:
        return self.sdk["profile"] == "integrated-p007"

    def write(self, name: str, report: dict) -> None:
        write_json(self.path / name, report | {"replay_sdk": self.sdk})


def sdk_provenance(profile: str) -> dict[str, Any]:
    assert bricklayer.__file__ is not None
    package = Path(bricklayer.__file__).resolve().parent
    ldraw = package / "ldraw"
    catalog_sha = sha256(
        (ldraw / f"official_catalog_{OFFICIAL_PARTS_VERSION}.json").read_bytes()
    ).hexdigest()
    expected = CATALOG_PROFILES[profile]
    if catalog_sha != expected:
        raise SystemExit(
            f"{profile} requires catalog {expected}; installed catalog is {catalog_sha}"
        )

    # Include implementation and packaged data, independent of checkout location.
    sdk_digest = sha256()
    for source in sorted(package.rglob("*")):
        relative = source.relative_to(package)
        if source.is_file() and "__pycache__" not in relative.parts:
            with source.open("rb") as stream:
                digest = file_digest(stream, "sha256").hexdigest()
            sdk_digest.update(f"{relative.as_posix()}\0{digest}\n".encode())

    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=package,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "profile": profile,
        "version": version("bricklayer-sdk"),
        "package_source": str(package),
        "git_revision": revision.stdout.strip() or None,
        "package_sha256": sdk_digest.hexdigest(),
        "catalog_version": OFFICIAL_PARTS_VERSION,
        "catalog_sha256": catalog_sha,
        "connector_data_sha256": sha256(
            (ldraw / "part_connectivity.json").read_bytes()
        ).hexdigest(),
    }


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
    output: ReplayOutput,
    name: str,
    record: Record,
    state: dict,
    report: StructureReport,
) -> dict:
    output.write(f"{name}/structure.json", report.model_dump(mode="json"))
    output.write(f"{name}/status.json", state)
    (output.path / name / "plan.json").write_bytes(
        (record.path / "plan.json").read_bytes()
    )
    validation = json.loads(record.report_path.read_bytes())
    native_checks = {
        "ok": validation["ok"],
        "source_sha256": validation["source_sha256"],
        "checks": validation["checks"],
    }
    output.write(f"{name}/native-validation.json", native_checks)
    write_json(record.path / "replay-sdk.json", output.sdk)
    return summarize(report) | {
        "record": record.id,
        "snapshot_id": state["snapshot_id"],
        "native_checks": native_checks,
    }


def replay_standard_wheel_trials(
    mpd: Path,
    column: Column,
    output: ReplayOutput,
    name: str,
) -> dict:
    model = read_ldraw(mpd)
    leaves = model.flattened_placements
    wheels = {}
    for key, pair in column.interface_evidence.items():
        parent, child = leaves[pair["host"]], leaves[pair["child"]]
        assert parent.content.ldraw_id == "56904" and child.content.ldraw_id == "30699"
        scratch = LegoBuild()
        mount = scratch.add_raw(parent.content, parent.pose)
        candidates = scratch.find_connection_candidates(
            mount, content=child.content, limit=33
        )
        trials = scratch.trial_connections(candidates, limit=33)
        assert len(trials) == 32
        checks = []
        for trial in trials:
            issue_codes = [issue.code for issue in trial.diagnostics]
            assert issue_codes == (
                [] if output.repaired_wheels else ["assembly.collision"]
            )
            pose = trial.candidate.pose
            assert pose is not None
            checks.append(
                {
                    "position": tuple(float(value) for value in pose.translation),
                    "rotation": pose.rotation.entries,
                    "diagnostic_codes": issue_codes,
                }
            )
        wheels[key] = {
            "host": selector(parent),
            "child": selector(child),
            "trials": checks,
        }
    output.write(f"{name}/standard-wheel-trials.json", {"interfaces": wheels})
    return {
        "interfaces": len(wheels),
        "passing": len(wheels) * 32 if output.repaired_wheels else 0,
        "collisions": 0 if output.repaired_wheels else len(wheels) * 32,
    }


def replay_reference(
    task_dir: Path, config: Config, output: ReplayOutput
) -> tuple[dict, dict]:
    task_path = task_dir / "environment/input/task.json"
    task = json.loads(task_path.read_bytes())
    task_id = task["id"]
    source = task_dir / "solution/reference.mpd"
    source_bytes = source.read_bytes()
    source_copy = output.path / "reference" / task_id / "original.mpd"
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
    output.write(f"{name}/original-structure.json", report.model_dump(mode="json"))
    assert report.complete and product.complete, (summary, summarize(report))
    assert report.native is not None
    parts, components = TASKS[task_id]
    if task_id == BUS and output.repaired_wheels:
        components = 10
    for column in (report.native, standard):
        assert column.part_count == parts
        assert len(column.components) == components
        assert not column.collisions and not column.floating
    expected = {
        BUS: {"unknown": 9 if output.repaired_wheels else 13},
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
        assert all(
            pair["mated"] is output.repaired_wheels
            for pair in standard.interface_evidence.values()
        )
        summary["standard_wheel_trials"] = replay_standard_wheel_trials(
            source_copy, standard, output, name
        )
    assert report.passed is product.passed is (components == 1)
    return summary | {"original": summarize(report)}, conditions


def replay_archive(
    baseline: Path, task_dir: Path, config: Config, output: ReplayOutput
) -> tuple[Record, dict, dict, StructureReport]:
    task_id = task_dir.name
    task_path = task_dir / "environment/input/task.json"
    archived = (
        baseline / "tasks" / task_id / "trial-1/attempt-1/artifacts/logs/artifacts"
    )
    (original_source,) = sorted((archived / "records").glob("*/build.py"))
    original_mpd = (archived / "model.mpd").read_bytes()
    name = f"archive/{task_id}"
    (output.path / name).mkdir(parents=True, exist_ok=True)
    (output.path / name / "original.mpd").write_bytes(original_mpd)
    (output.path / name / "original-build.py").write_bytes(original_source.read_bytes())

    record = Record.create(
        config, prompt=f"Archive replay {task_id}", task_path=task_path
    )
    features = features_for(json.loads(task_path.read_bytes()))
    assert not codes(set_artifact(record, "features", features))
    replay_source = original_source.read_text()
    for filename in ("palette.csv", "task.json"):
        replacement = repr(str(task_path.parent / filename))
        for quote in ('"', "'"):
            replay_source = replay_source.replace(
                f"{quote}/app/input/{filename}{quote}", replacement
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
    repaired = output.repaired_wheels
    native_groups = BUS_DISCONNECTED if repaired else [1007]
    standard_groups = [1007] if repaired else BUS_DISCONNECTED
    assert summary["native"]["components"] == (
        HOUSE_COMPONENTS if task_id == HOUSE else native_groups
    ), summary
    assert summary["standard"]["components"] == (
        HOUSE_COMPONENTS if task_id == HOUSE else standard_groups
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
        assert native["mated"] is (task_id == BUS and not repaired)
        assert standard["mated"] is (task_id == BUS and repaired)
        if task_id == HOUSE:
            assert standard["direct_contact"] and standard["search_complete"]
            assert standard["candidate_count"] == 0
        else:
            assert native["host_endpoints"] != standard["host_endpoints"]
    if task_id == BUS:
        summary["standard_wheel_trials"] = replay_standard_wheel_trials(
            output.path / name / "original.mpd", report.standard, output, name
        )
        assert summary["native_checks"]["ok"] is (not repaired)
        assert "structure.unattached_component" in codes(state)
        with patch("brickwright.flow.state.review_status", return_value=FIXTURE_REVIEW):
            blocked = finalize(record, config)
        assert not blocked.ok and not record.ldr_path.exists()
        assert blocked.flow and "flow.structure" in codes(blocked.flow)
        output.write(f"{name}/fixture-finalize.json", blocked.model_dump(mode="json"))
        if repaired:
            summary["without_legacy_override"] = replay_bus_without_override(
                record, config, plan, output, original_mpd
            )
    return record, plan, summary, report


def replay_bus_without_override(
    archived: Record,
    config: Config,
    plan: dict,
    output: ReplayOutput,
    original_mpd: bytes,
) -> dict:
    source = archived.read_build()
    obsolete = "        if element.ldraw_id == '56904':\n            element = WheelRim(element.ldraw_id, element.color)\n"
    assert source.count(obsolete) == 1
    record = Record.create(
        config,
        prompt="Archive bus without record-local wheel offset",
        task_path=archived.path / "task.json",
    )
    task = json.loads((archived.path / "task.json").read_bytes())
    assert not codes(set_artifact(record, "features", features_for(task)))
    record.write_build(source.replace(obsolete, ""))
    state, report = saved_structure(record, config, plan)
    assert state["structural_passed"] and report.passed
    assert report.native is not None and report.standard is not None
    for column in (report.native, report.standard):
        assert [component.part_count for component in column.components] == [1007]
        assert all(pair["mated"] for pair in column.interface_evidence.values())
        assert not column.collisions and not column.floating
    assert not report.limitations
    assert Path(state["mpd_path"]).read_bytes() == original_mpd
    return save_case(
        output, f"archive/{BUS}-without-legacy-override", record, state, report
    )


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
    output: ReplayOutput,
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
        output.write(f"boundary/{name}/structure.json", result.model_dump(mode="json"))
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
        output.write(f"boundary/{name}/structure.json", result.model_dump(mode="json"))
        (output.path / "boundary" / name / "changed.mpd").write_bytes(incomplete)
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


def replay_finalize(
    record: Record, plan: dict, config: Config, output: ReplayOutput
) -> dict:
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
            (output.path / "boundary" / "fixture-delivered.mpd").write_bytes(
                record.ldr_path.read_bytes()
            )
        outcomes[name] = {
            "fixture_review": FIXTURE_REVIEW,
            "structure": summarize(report),
            "status_before_fixture": state,
            "finalize": result.model_dump(mode="json"),
        }
    output.write("boundary/fixture-finalize.json", outcomes)
    return {name: outcome["finalize"]["ok"] for name, outcome in outcomes.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--ldraw-library", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sdk-profile", required=True, choices=CATALOG_PROFILES)
    args = parser.parse_args()
    tasks = args.dataset.resolve(strict=True) / "harbor-tasks"
    path = args.output.resolve() / args.sdk_profile
    output = ReplayOutput(path, sdk_provenance(args.sdk_profile))
    output.path.mkdir(parents=True, exist_ok=False)
    os.environ["LDRAW_LIBRARY_PATH"] = str(args.ldraw_library.resolve(strict=True))
    config = Config(records_dir=output.path / "records", flow=True, build_timeout=90)
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
    output.write("summary.json", results)
    print(output.path / "summary.json")


if __name__ == "__main__":
    main()
