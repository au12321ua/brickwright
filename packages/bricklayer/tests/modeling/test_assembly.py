"""Connection discovery and checked placement through the public SDK."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest

import bricklayer as bl
from bricklayer.geometry.connections import connectors_mate


def part(part_id: str) -> bl.Element:
    return bl.Element(part_id, bl.Color.WHITE)


def test_hinge_discovery_seats_local_interfaces_in_a_stable_order():
    first = bl.find_connection_pairs(
        "rotate", parent_parts=("4275b",), child_parts=("4276b",)
    )
    second = bl.find_connection_pairs(
        bl.AssemblyIntent.ROTATE, parent_parts=("4275b",), child_parts=("4276b",)
    )
    assert first == second
    (pair,) = first
    assert pair.parent.part_id == "4275b"
    assert pair.child.part_id == "4276b"
    assert pair.joint_kind is bl.JointKind.REVOLUTE
    assert pair.requires_trial
    assert connectors_mate(
        pair.parent.connector, pair.child.connector.transformed(pair.pose)
    )


def test_catalog_side_discovery_filters_by_local_facing_and_inventory():
    pairs = bl.find_connection_pairs(
        "side",
        parent_parts=("11211",),
        child_parts=("3024",),
        facing=bl.Direction.FRONT,
        limit=1,
    )
    (pair,) = pairs
    assert pair.parent.connector.axis == bl.Direction.FRONT.value
    assert pair.child.connector.type.name == "antistud"
    assert not bl.find_connection_pairs(
        "side",
        parent_parts=("11211",),
        child_parts=("3024",),
        facing=bl.Direction.UP,
    )


def test_repeated_submodels_offer_their_own_free_world_endpoints():
    insert = bl.LegoBuild()
    block = insert.add(part("11211"), name="block")
    module = bl.LegoBuild()
    inner = module.add_raw(insert, name="insert")

    model = bl.LegoBuild()
    left = model.add_raw(module, name="left")
    right = model.add_raw(
        module, bl.Pose(bl.Rotation.DEG_90.matrix, bl.Position(8, 0, 0)), name="right"
    )
    facing = bl.Rotation.DEG_90.matrix @ bl.Direction.FRONT.value
    (left_candidate,) = model.find_connection_candidates(
        ["left", "insert"], "side", part_ids=("3024",), limit=1
    )
    panel = model.place_connection(left_candidate, name="left_panel")

    (candidate,) = model.find_connection_candidates(
        model.named(["right", "insert"]),
        "side",
        part_ids=("3024",),
        facing=facing,
        toward=bl.Position(8, 0, 0),
        limit=1,
    )
    right_panel = model.place_connection(candidate, name="right_panel")
    joint = model.joint(right_panel)
    assert joint.parent.path == (right, inner, block)
    assert model.joint(panel).parent.path == (left, inner, block)
    assert joint.parent.connector.transformed(joint.parent.pose).axis == facing
    assert connectors_mate(
        joint.parent.connector.transformed(joint.parent.pose),
        joint.child.connector.transformed(joint.child.pose),
    )
    assert model.is_valid()


def test_toward_ranks_host_endpoints_ahead_of_catalog_preference():
    model = bl.LegoBuild()
    model.add_raw(part("11211"), name="host")
    point = bl.Position(0.5, -0.5, 7 / 12)
    (candidate,) = model.find_connection_candidates(
        "host",
        "side",
        part_ids=("3024",),
        facing=bl.Direction.FRONT,
        toward=point,
        limit=1,
    )
    assert candidate.parent.connector.position == point


def test_hatch_opens_up_sixty_degrees_and_trials_leave_state_unchanged():
    model = bl.LegoBuild()
    frame = model.add(part("4275b"), name="frame")
    model.add_step()
    door = bl.LegoBuild()
    leaf = door.add(part("4276b"), name="hinge")
    door.stack(bl.plate(2, 1), on=leaf, name="skin")

    candidates = model.find_connection_candidates(
        "frame",
        "rotate",
        content=door,
        facing=bl.Direction.BACK,
        mountings=(bl.Mounting(angle=90), bl.Mounting(angle=180)),
        motion=bl.MotionRange(bl.Revolute(-60), bl.Revolute(0)),
        opening_toward=bl.Direction.UP,
        opening_point=door.named("skin").center,
        limit=5,
    )
    (trial,) = model.trial_connections(candidates)
    assert trial.passed, trial.diagnostics
    assert trial.checks == ("endpoints", "mating", "collision", "joint_kind", "motion")
    assert trial.motion_report is not None
    assert trial.motion_report.samples_checked == 5
    assert model.placements == (frame,)
    assert not model.joints
    assert tuple(model.names) == ("frame",)
    assert model.step_markers == (1,)
    assert door.brick_count == 2

    hatch = model.place_connection(trial.candidate, name="hatch")
    joint = model.joint(hatch)
    assert joint.child.path == (hatch, leaf)
    assert hatch.pose == trial.candidate.pose
    rest = model.named(["hatch", "skin"])
    at_60 = model.posed({joint: bl.Revolute(-60)})
    assert at_60.placements[-1].center.z > rest.center.z
    assert model.check_motion().passed
    assert model.is_valid()


def test_motion_interference_reports_sample_and_both_parts():
    model = bl.LegoBuild()
    model.add(part("4275b"), name="frame")
    (candidate,) = model.find_connection_candidates(
        "frame",
        "rotate",
        content=part("4276b"),
        mountings=(bl.Mounting(angle=270),),
        motion=bl.MotionRange(bl.Revolute(0), bl.Revolute(60)),
    )
    (trial,) = model.trial_connections((candidate,))
    assert trial.checks == ("endpoints", "mating", "collision", "joint_kind", "motion")
    assert not trial.passed
    (issue,) = trial.diagnostics
    assert issue.code == "motion.collision"
    example = cast("list[dict[str, Any]]", issue.evidence["samples"])[0]
    assert {part["part_id"] for part in example["placements"]} == {"4275b", "4276b"}
    assert example["positions"][0]["coordinates"]["angle"] == 30
    assert example["placements"][0]["path"] == ["frame"]
    assert model.brick_count == 1
    assert not model.joints


def test_explicit_interference_leaves_everything_intact_and_can_be_retried():
    model = bl.LegoBuild()
    beam = model.add_raw(part("7229"), name="beam")
    model.add_step()
    candidates = model.find_connection_candidates(
        beam,
        "slide",
        content=part("3705"),
        parent_type="axle_hole",
        child_type="axle",
        mountings=(bl.Mounting(angle=45), bl.Mounting()),
    )
    (blocked,) = model.trial_connections(candidates, limit=1)
    assert blocked.diagnostics[0].code == "assembly.collision"
    with pytest.raises(bl.AssemblyError) as exc:
        model.place_connection(blocked.candidate, name="axle", index=0)
    assert exc.value.code == "assembly.collision"
    assert not exc.value.trial.passed
    assert model.placements == (beam,)
    assert model.step_markers == (1,)
    assert tuple(model.names) == ("beam",)
    assert not model.joints

    good = model.trial_connections(candidates)[1]
    assert good.passed
    axle = model.place_connection(good.candidate, name="axle", index=0)
    assert model.placements == (axle, beam)
    assert model.step_markers == (2,)
    assert model.joint(axle).kind is bl.JointKind.PRISMATIC


def test_commit_rechecks_occupied_and_replaced_targets():
    model = bl.LegoBuild()
    host = model.add(part("11211"), name="block")
    (candidate,) = model.find_connection_candidates(
        "block", "side", content=part("3024"), limit=1
    )
    assert model.trial_connections((candidate,))[0].passed

    placed = model.place_connection(candidate)
    with pytest.raises(bl.AssemblyError) as exc:
        model.place_connection(candidate)
    assert exc.value.code == "connector.occupied"
    assert model.brick_count == 2
    assert model.joints == (model.joint(placed),)

    model.remove(host)
    model.add(part("11211"), name="block")
    with pytest.raises(bl.AssemblyError) as exc:
        model.place_connection(candidate)
    assert exc.value.code == "connector.stale"
    assert model.brick_count == 2
    assert not model.joints


def test_new_obstacle_after_trial_prevents_commit():
    model = bl.LegoBuild()
    model.add(part("11211"), name="block")
    (candidate,) = model.find_connection_candidates(
        "block", "side", content=part("3024"), limit=1
    )
    assert model.trial_connections((candidate,))[0].passed
    obstacles = bl.LegoBuild()
    obstacles.add_raw(part("3005"), bl.Pose(translation=bl.Position(0, -0.5, 0)))
    assert candidate.pose is not None
    model.add_raw(obstacles, candidate.pose, name="obstacles")
    with pytest.raises(bl.AssemblyError) as exc:
        model.place_connection(candidate)
    assert exc.value.code == "assembly.collision"
    assert not model.joints


def test_concurrent_checked_placements_cannot_consume_one_stud_twice():
    model = bl.LegoBuild()
    model.add(part("11211"), name="block")
    (candidate,) = model.find_connection_candidates(
        "block", "side", part_ids=("3024",), limit=1
    )

    def place(_: int) -> str:
        try:
            model.place_connection(candidate)
        except bl.AssemblyError as error:
            return error.code
        return "placed"

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = sorted(workers.map(place, range(2)))
    assert outcomes == ["connector.occupied", "placed"]
    assert model.brick_count == 2
    assert len(model.joints) == 1


def test_sliding_checks_candidate_and_existing_hold_along_a_long_bar():
    model = bl.LegoBuild()
    bar = model.add_raw(part("30374"), name="rail")
    target = bar.find_connector("bar")
    model.connect(part("30237"), "clip", target, mounting=bl.Mounting(offset=-1))

    candidates = model.find_connection_candidates(
        "rail",
        "clip",
        part_ids=("30237",),
        mountings=(bl.Mounting(offset=-1), bl.Mounting(offset=1)),
    )
    occupied, free = model.trial_connections(candidates)
    assert occupied.diagnostics[0].code == "connector.occupied"
    assert free.passed
    assert free.candidate.slide_ranges
    model.place_connection(free.candidate, name="second_clip")
    assert model.is_valid()
    assert model.is_connected()


def test_second_axle_can_use_opposite_end_of_an_occupied_hole():
    model = bl.LegoBuild()
    model.add_raw(part("7229"), name="beam")

    def candidate(offset):
        (found,) = model.find_connection_candidates(
            "beam",
            "slide",
            part_ids=("3705",),
            parent_type="axle_hole",
            child_type="axle",
            mountings=(bl.Mounting(offset=offset),),
        )
        return found

    model.place_connection(candidate(-2))
    assert model.connection_endpoints("beam", "axle_hole")
    model.place_connection(candidate(2))
    assert model.is_connected() and model.is_valid()


def test_default_seating_accounts_for_the_whole_requested_slide():
    model = bl.LegoBuild()
    bar = model.add_raw(part("30374"), name="rail")
    target = bar.find_connector("bar")
    model.connect(part("30237"), "clip", target, mounting=bl.Mounting(offset=-1.5))
    motion = bl.MotionRange(bl.Cylindrical(-1.5), bl.Cylindrical())
    candidates = model.find_connection_candidates(
        "rail",
        "slide",
        content=part("30237"),
        parent_type="bar",
        child_type="clip",
        motion=motion,
    )
    passing = next(t.candidate for t in model.trial_connections(candidates) if t.passed)
    model.place_connection(passing)
    assert model.check_motion().passed


def test_duplicate_legacy_annotations_are_one_physical_endpoint():
    model = bl.LegoBuild()
    bar = model.add_raw(part("30374"), name="rail")
    candidates = model.find_connection_candidates(
        bar,
        "clip",
        content=part("3626"),
        parent_type="bar",
        child_type="clip",
        mountings=(bl.Mounting(),),
    )
    (trial,) = model.trial_connections(candidates)
    assert trial.checks == ("endpoints", "mating", "collision")


def test_trial_enforces_requested_joint_kind_even_for_static_placement():
    model = bl.LegoBuild()
    model.add_raw(part("7229"), name="beam")
    (candidate,) = model.find_connection_candidates(
        "beam",
        "slide",
        content=part("3705"),
        parent_type="axle_hole",
        child_type="axle",
        joint_kind=bl.JointKind.CYLINDRICAL,
        mountings=(bl.Mounting(),),
    )
    (trial,) = model.trial_connections((candidate,))
    assert not trial.passed
    (issue,) = trial.diagnostics
    assert issue.code == "connector.motion_kind"
    assert issue.evidence["actual"] == "PRISMATIC"
    assert issue.evidence["expected"] == "CYLINDRICAL"
    assert not model.joints


def test_mounting_outside_the_joints_freedom_reports_unsolved_pose():
    model = bl.LegoBuild()
    model.add(part("4275b"), name="hinge")
    (candidate,) = model.find_connection_candidates(
        "hinge", "rotate", content=part("4276b"), mountings=(bl.Mounting(offset=0.5),)
    )
    assert candidate.pose is None
    (trial,) = model.trial_connections((candidate,))
    assert not trial.passed
    assert trial.checks == ("endpoints", "mating")
    assert trial.diagnostics[0].code == "connector.unsolved_pose"


@pytest.mark.parametrize("intent", ("side", "rotate", "ball", "pin", "clip", "slide"))
def test_unrestricted_discovery_offers_a_trialable_part_pair(intent):
    (pair,) = bl.find_connection_pairs(intent, limit=1)
    assert pair.verification_scope
    assert pair.requires_trial

    model = bl.LegoBuild()
    model.add(part(pair.parent.part_id), name="host")
    motion = (
        bl.MotionRange(bl.Revolute(-60), bl.Revolute(0)) if intent == "rotate" else None
    )
    candidates = model.find_connection_candidates("host", intent, motion=motion)
    results = model.trial_connections(candidates)
    assert results[0].passed, results[0].diagnostics
    assert results[0].candidate.child.part_id == pair.child.part_id
    model.place_connection(results[0].candidate)
    assert model.is_connected() and model.is_valid()


@pytest.mark.parametrize("intent", ("rotate", "ball", "pin", "clip", "slide"))
def test_catalog_verified_pose_is_available_from_either_end(intent):
    (pair,) = bl.find_connection_pairs(intent, limit=1)
    model = bl.LegoBuild()
    model.add(part(pair.child.part_id), name="host")
    trials = model.trial_connections(model.find_connection_candidates("host", intent))
    assert trials[0].passed, trials[0].diagnostics
    assert trials[0].candidate.child.part_id == pair.parent.part_id


def test_pin_holes_and_ball_members_are_discoverable():
    model = bl.LegoBuild()
    beam = model.add(part("32316"), name="beam")
    (pin,) = model.find_connection_candidates(
        "beam",
        "pin",
        part_ids=("2780",),
        toward=beam.center,
        mountings=(bl.Mounting(),),
        limit=1,
    )
    assert pin.parent.connector.position.y == beam.center.y
    model.place_connection(pin)
    assert model.is_connected() and model.is_valid()

    model = bl.LegoBuild()
    model.add(part("14418"), name="socket")
    (ball,) = model.find_connection_candidates(
        "socket",
        "ball",
        part_ids=("22890",),
        target_rotation=bl.Rotation.DEG_180,
        mountings=(bl.Mounting(swing_x=5),),
        limit=1,
    )
    assert ball.pose is not None
    assert bl.Rotation.DEG_180.matrix != ball.pose.rotation
    handle = model.place_connection(ball)
    assert model.joint(handle).kind is bl.JointKind.BALL
    assert model.is_connected() and model.is_valid()


def test_grid_wall_can_still_be_placed_without_a_connection_trial():
    model = bl.LegoBuild()
    lower = model.add(bl.brick(2, 1))
    model.stack(bl.brick(2, 1), on=lower)
    assert model.is_valid() and model.is_connected()
    assert not model.joints
