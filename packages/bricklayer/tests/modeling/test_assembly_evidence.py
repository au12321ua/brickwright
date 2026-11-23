"""Assembly reports for the limiting cases found during the OMR36 runs."""

import json
from dataclasses import replace
from typing import Any

import pytest

import bricklayer as bl
from bricklayer.geometry.linalg import all_close
from bricklayer.ldraw.conversion import pose_from_type1


def test_window_reports_an_incompatible_pair_without_trialing(monkeypatch):
    model = bl.LegoBuild()
    frame = model.add_raw(
        bl.Element("60594", bl.Color.WHITE),
        pose_from_type1([260, -152, -230, 1, 0, 0, 0, 1, 0, 0, 0, 1]),
        name="frame",
    )
    leaf = bl.Element("60608", bl.Color.GREEN)
    target = pose_from_type1([228, -148, -226, 0, 0, -1, 0, 1, 0, 1, 0, 0])
    query: dict[str, Any] = {}
    candidates = model.find_connection_candidates(
        frame, content=leaf, target_rotation=target.rotation, evidence=query
    )
    assert candidates == ()
    assert query["code"] == "assembly.no_candidates"
    assert query["stopped_at"] == "mating_rule"
    assert query["limit"] == 20 and query["returned"] == 0
    assert query["parent"]["count"] == 17
    assert any(
        row["path"] == ["frame"] and row["local_connector"]["type"] == "hinge_18_on"
        for row in query["parent"]["examples"]
    )
    assert query["child"]["count"] == 1
    assert query["child"]["examples"][0]["local_connector"]["type"] == "hinge_17_in"

    def unexpected(*args, **kwargs):
        raise AssertionError("empty input must never reach a trial")

    monkeypatch.setattr("bricklayer.modeling.build.trial_connection", unexpected)
    batch: dict[str, Any] = {}
    assert model.trial_connections(candidates, evidence=batch) == ()
    assert batch["limit"] == 8 and batch["checked"] == 0
    assert batch["failure_counts"] == {}
    assert model.placements == (frame,)
    json.dumps([query, batch])


def test_collision_batch_reports_budget_live_fit_endpoints_and_pair():
    model = bl.LegoBuild()
    host = model.add(bl.Element("4275b", bl.Color.WHITE), name="host")
    leaf = bl.Element("4276b", bl.Color.BLACK)
    (candidate,) = model.find_connection_candidates(
        host, "rotate", content=leaf, mountings=(bl.Mounting(angle=180),)
    )
    fitted = candidate.pose
    assert fitted is not None
    target_position = fitted.apply(candidate.child.connector.position) + bl.Position(
        0.1, 0, 0
    )
    candidate = replace(candidate, target_position=target_position)
    obstacle = model.add_raw(
        bl.Element("3024", bl.Color.WHITE), fitted, name="obstacle"
    )
    batch: dict[str, Any] = {}
    trials = model.trial_connections((candidate, candidate), limit=1, evidence=batch)
    assert len(trials) == 1 and not trials[0].passed
    assert batch["code"] == "assembly.all_colliding"
    assert batch["limit"] == 1 and batch["offered"] == 2 and batch["checked"] == 1
    assert batch["passed"] == 0 and batch["failure_counts"] == {"assembly.collision": 1}
    (example,) = batch["examples"]
    assert example["rank"] == 1
    (error,) = example["diagnostics"]
    assert error["evidence"]["parent"]["path"] == ["host"]
    assert error["evidence"]["child"]["part_id"] == "4276b"
    assert error["evidence"]["fitted_pose"]["rotation"] == list(
        fitted.rotation.flatten()
    )
    assert any(
        p["path"] == ["obstacle"]
        for p in error["evidence"]["examples"][0]["placements"]
    )
    assert error["evidence"]["target_error"][
        "connector_distance_studs"
    ] == pytest.approx(0.1)
    assert model.placements == (host, obstacle)
    json.dumps(batch)

    def stream():
        yield candidate
        raise AssertionError("trial consumed a candidate past its budget")

    assert len(model.trial_connections(stream(), limit=1, evidence=batch)) == 1
    assert batch["offered"] is None
    assert model.trial_connections(stream(), limit=0, evidence=batch) == ()
    assert batch["code"] == "assembly.no_trials"
    assert batch["failure_counts"] == {}


def test_tilted_hinge_fit_matches_a_known_pose_in_both_rotation_and_translation():
    model = bl.LegoBuild()
    host = model.add_raw(
        bl.Element("3937", bl.Color.BLACK),
        pose_from_type1([-40, -68, 340, 0, 0, -1, 0, 1, 0, 1, 0, 0]),
    )
    leaf = bl.Element("6134", bl.Color.WHITE)
    pivot = host.find_connector("hinge_12_on")
    source = leaf.find_connector("hinge_12_in")
    rest = model.copy().connect(leaf, connector=source, to=pivot).pose
    target_rotation = bl.rotation_about(pivot.axis, 22.5) @ rest.rotation
    expected_position = pivot.position - bl.Pose(target_rotation).apply(source.position)
    query: dict[str, Any] = {}
    candidates = model.find_connection_candidates(
        host,
        content=leaf,
        parent_type=iter(("hinge_12_on",)),
        child_type=iter(("hinge_12_in",)),
        target_rotation=target_rotation,
        evidence=query,
    )
    batch: dict[str, Any] = {}
    trials = model.trial_connections(candidates, evidence=batch)
    assert query["limit"] == 20 and query["returned"] == len(candidates) == 1
    assert query["request"]["parent_type"] == ["hinge_12_on"]
    assert query["request"]["child_type"] == ["hinge_12_in"]
    assert batch["limit"] == 8 and batch["checked"] == 1
    assert any(
        t.passed
        and t.candidate.pose is not None
        and all_close(t.candidate.pose.rotation, target_rotation, tol=0.00001)
        and (t.candidate.pose.translation - expected_position).to_stud_units().norm()
        < 0.00001
        for t in trials
    )
