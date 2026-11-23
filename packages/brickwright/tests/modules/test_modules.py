import json
from hashlib import sha256

import bricklayer as bl
import pytest
from bricklayer.io import from_ldraw, to_ldraw

from brickwright.config import Config
from brickwright.modules import (
    ModuleError,
    check_module,
    init_module,
    load_merge,
    merge_context,
    merge_evidence,
    module_instructions,
    module_status,
    place_module,
    prepare_merge,
    validate_contracts,
)
from brickwright.record import Record

IDENTITY = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]


def _module(name, low_y, high_y, origin):
    return {
        "id": name,
        "occupancy": {"min": [-10, low_y, -10], "max": [10, high_y, 10]},
        "origin": [0, origin, 0],
        "rotation": IDENTITY,
        "palette": [4, 15],
        "part_budget": 4,
    }


def _plate_plan():
    return {
        "modules": [
            _module("lower", -8, 0, 0),
            _module("upper", -20, -8, -8),
            _module("side", -8, 0, 0)
            | {"occupancy": {"min": [10, -8, -10], "max": [30, 0, 10]}},
        ],
        "interfaces": [
            {
                "id": "decks",
                "modules": ["lower", "upper"],
                "quantity": 1,
                "joints": [
                    {
                        "a": {
                            "role": "mount",
                            "part_id": "3024",
                            "type": "stud",
                            "position": [0, -8, 0],
                            "axis": [0, -1, 0],
                        },
                        "b": {
                            "role": "floor",
                            "part_id": "3024",
                            "type": "antistud",
                            "position": [0, -8, 0],
                            "axis": [0, -1, 0],
                        },
                        "intrusion": {
                            "from_side": "a",
                            "position": [0, -8, 0],
                            "direction": [0, -1, 0],
                            "half_widths": [6, 6],
                            "max_depth": 4,
                            "tolerance": 0.25,
                        },
                    }
                ],
            }
        ],
    }


def _save_plan(parent, plan):
    (parent.path / "plan.json").write_text(json.dumps(plan))


@pytest.fixture
def assembly(tmp_path):
    config = Config(records_dir=tmp_path)
    parent = Record.create(config)
    plan = _plate_plan()
    _save_plan(parent, plan)
    return config, parent, plan


def _plate():
    build = bl.LegoBuild()
    build.add_raw(bl.plate(1, 1, bl.Color.RED))
    return build


def _seal(child, build):
    digest, diagnostics = check_module(build, child.path)
    assert not diagnostics
    mpd = to_ldraw(build).encode()
    child.ldr_path.write_bytes(mpd)
    source_sha = child.source_sha256
    from brickwright.record import Status

    snapshot = child.add_snapshot(
        child.read_build(),
        json.dumps(
            {
                "ok": True,
                "source_sha256": source_sha,
                "module_contract_sha256": digest,
                "module_mpd_sha256": sha256(mpd).hexdigest(),
            }
        ),
    )
    child.update_meta(
        status=Status.VALID,
        validated_source_sha256=source_sha,
        exported_source_sha256=source_sha,
        finalized=True,
    )
    return snapshot


def test_rejects_body_overlap_and_interface_outside_shared_face():
    plan = _plate_plan()
    validate_contracts(plan)
    plan["modules"][2]["occupancy"]["min"][0] = 9
    with pytest.raises(ValueError, match="overlap"):
        validate_contracts(plan)
    plan["modules"][2]["occupancy"]["min"][0] = 10
    plan["interfaces"][0]["joints"][0]["intrusion"]["position"][1] = -9
    with pytest.raises(ValueError, match="shared face"):
        validate_contracts(plan)


def test_rotated_local_module_and_stack_stud_intrusion(assembly):
    config, parent, plan = assembly
    plan["modules"][0]["rotation"] = [[0, 0, 1], [0, 1, 0], [-1, 0, 0]]
    _save_plan(parent, plan)
    child = init_module(parent, "lower", config)
    from brickwright.tools.build_execution import load_build_module

    assert "local" in module_instructions(child)
    template = load_build_module(child.build_path)
    assert isinstance(template.create_build(), bl.LegoBuild)
    assert isinstance(template.run_tests(), list)
    assert check_module(_plate(), child.path)[1] == []
    plan["interfaces"][0]["joints"][0]["intrusion"]["max_depth"] = 2
    _save_plan(parent, plan)
    assert check_module(_plate(), child.path)[1][0].code == "module.contract_stale"
    child = init_module(parent, "lower", config)
    diagnostics = check_module(_plate(), child.path)[1]
    assert [d.code for d in diagnostics] == ["module.outside_occupancy"]
    assert diagnostics[0].evidence["direction"] == [0, -1, 0]


def test_wrong_endpoint_and_unrelated_geometry(assembly):
    config, parent, plan = assembly
    child = init_module(parent, "lower", config)
    moved = bl.LegoBuild()
    moved.add_raw(
        bl.plate(1, 1, bl.Color.RED),
        bl.Pose(bl.Pose.IDENTITY.rotation, bl.Position(0.05, 0, 0)),
    )
    assert {d.code for d in check_module(moved, child.path)[1]} == {
        "module.interface_missing",
        "module.outside_occupancy",
    }
    plan["interfaces"][0]["joints"][0]["a"]["part_id"] = "3005"
    _save_plan(parent, plan)
    child = init_module(parent, "lower", config)
    assert "module.outside_occupancy" in {
        d.code for d in check_module(_plate(), child.path)[1]
    }


def test_merge_stays_replayable_after_source_change_and_stales_only_interface_ends(
    assembly,
):
    config, parent, plan = assembly
    del plan["modules"][2]
    _save_plan(parent, plan)
    lower = init_module(parent, "lower", config)
    upper = init_module(parent, "upper", config)
    _seal(lower, _plate())
    _seal(upper, _plate())
    frozen = prepare_merge(parent)
    assert frozen is not None
    expected = _combine(frozen)
    assert "0 FILE lower.ldr" in expected and "0 FILE upper.ldr" in expected
    evidence, failures = merge_evidence(from_ldraw(expected), frozen)
    assert failures == []
    assert [e["interface"] for e in evidence] == ["decks"]

    lower.write_build(lower.read_build() + "\n# a newer source")
    assert module_status(parent)["modules"][0]["has_update"]
    with pytest.raises(ModuleError, match="finalize"):
        prepare_merge(parent)
    assert _combine(load_merge(parent, frozen.manifest_sha256)) == expected
    _seal(lower, _plate())
    replacement = prepare_merge(parent)
    assert replacement is not None
    assert replacement.manifest_sha256 != frozen.manifest_sha256
    assert _combine(frozen) == expected

    plan["modules"].append(_plate_plan()["modules"][2])
    _save_plan(parent, plan)
    side = init_module(parent, "side", config)
    original = (side.path / "module-contract.json").read_bytes()
    plan["interfaces"][0]["joints"][0]["intrusion"]["max_depth"] = 3
    _save_plan(parent, plan)
    statuses = module_status(parent)["modules"]
    assert [item["contract_stale"] for item in statuses] == [True, True, False]
    init_module(parent, "side", config)
    assert (side.path / "module-contract.json").read_bytes() == original


def test_merge_rejects_snapshot_tampering(assembly):
    config, parent, plan = assembly
    del plan["modules"][2]
    _save_plan(parent, plan)
    lower = init_module(parent, "lower", config)
    upper = init_module(parent, "upper", config)
    snapshot = _seal(lower, _plate())
    _seal(upper, _plate())
    frozen = prepare_merge(parent)
    assert frozen is not None
    snapshot.build_path.write_text("# different source")
    with pytest.raises(ModuleError, match="digest"):
        load_merge(parent, frozen.manifest_sha256)


def _combine(frozen):
    build = bl.LegoBuild()
    with merge_context(frozen):
        place_module(build, "lower")
        place_module(build, "upper")
    return to_ldraw(build)


def test_module_dispatch_hands_back_child_snapshot(assembly, monkeypatch):
    from brickwright.driver import loop
    from brickwright.driver.report import RunReport
    from brickwright.tools.registry import available_tools
    from brickwright.tools.subagents import SubagentTask, dispatch_subagents

    config, parent, _ = assembly
    seen = []

    def run(child, cfg, *, narrator):
        assert child.path == parent.path / "modules" / "lower"
        assert "local" in child.read_meta().prompt
        assert not cfg.can_dispatch_subagents
        assert "finalize" in {item["function"]["name"] for item in available_tools(cfg)}
        seen.append(_seal(child, _plate()).name)
        return RunReport(done=True, turns=1, validations=1)

    monkeypatch.setattr(loop, "run_agent", run)
    output = dispatch_subagents(
        parent, [SubagentTask(prompt="Build lower deck", module="lower")], config
    )
    result = output[0]
    assert result.ok and result.valid and result.finalized
    assert result.record_id == parent.id + "/modules/lower"
    assert result.snapshot_id == seen[0]
    assert result.session_id
    assert module_status(parent)["modules"][0]["sessions"][0]["self_check_passed"]


def test_pin_joint_mates_in_declared_space_and_reports_real_collision(tmp_path):
    from bricklayer.ldraw.conversion import pose_from_type1

    config = Config(records_dir=tmp_path)
    parent = Record.create(config)
    plan = {
        "modules": [
            _module("pin-side", -32, 0, 0)
            | {"occupancy": {"min": [-25, -32, -10], "max": [2, 0, 10]}},
            _module("hole-side", -32, 0, 0)
            | {"occupancy": {"min": [2, -32, -10], "max": [25, 0, 10]}},
        ],
        "interfaces": [
            {
                "id": "pin-brick",
                "modules": ["pin-side", "hole-side"],
                "quantity": 1,
                "joints": [
                    {
                        "a": {
                            "role": "drive",
                            "part_id": "2780",
                            "type": "pin",
                            "position": [11, -14, 0],
                            "axis": [1, 0, 0],
                        },
                        "b": {
                            "role": "bore",
                            "part_id": "6541",
                            "type": "pin_hole",
                            "position": [12, -14, 0],
                            "axis": [1, 0, 0],
                        },
                        "intrusion": {
                            "from_side": "a",
                            "position": [2, -14, 0],
                            "direction": [1, 0, 0],
                            "half_widths": [6.25, 6.25],
                            "max_depth": 18,
                            "tolerance": 0.25,
                        },
                    }
                ],
            }
        ],
    }
    _save_plan(parent, plan)
    pin = init_module(parent, "pin-side", config)
    hole = init_module(parent, "hole-side", config)
    source = bl.LegoBuild()
    source.add_raw(
        bl.Element("2780", bl.Color.RED),
        pose_from_type1([0, -14, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]),
    )
    receiver = bl.LegoBuild()
    receiver.add_raw(
        bl.Element("6541", bl.Color.RED),
        pose_from_type1([12, 0, 0, 0, 0, 1, 0, 1, 0, -1, 0, 0]),
    )
    _seal(pin, source)
    _seal(hole, receiver)
    frozen = prepare_merge(parent)
    assert frozen is not None
    model = bl.LegoBuild()
    with merge_context(frozen):
        place_module(model, "pin-side")
        place_module(model, "hole-side")
    jointly, diagnostics = merge_evidence(from_ldraw(to_ldraw(model)), frozen)
    assert diagnostics == []
    assert jointly[0]["interface"] == "pin-brick"
    model.add_raw(bl.Element("6541", bl.Color.RED), receiver.placements[0].pose)
    _, diagnostics = merge_evidence(model, frozen)
    assert any(
        issue.code == "module.collision"
        and issue.evidence["modules"] == ["hole-side", None]
        for issue in diagnostics
    )
