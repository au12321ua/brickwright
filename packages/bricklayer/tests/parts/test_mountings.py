"""Replicate the geometric evidence for each P-003 part pair."""

import json

import pytest

from bricklayer import (
    Ball,
    Color,
    Cylindrical,
    Direction,
    Element,
    JointKind,
    LegoBuild,
    MotionRange,
    Mounting,
    PartSpec,
    Pose,
    Position,
    Revolute,
    from_ldraw,
    part_exists,
    rotation_about,
    search_parts,
    to_ldraw,
)
from bricklayer.geometry.connections import connectors_mate
from bricklayer.ldraw import partdata
from bricklayer.ldraw.conversion import pose_to_ldraw
from bricklayer.ldraw.meshes import DATA_DIR
from bricklayer.parts.mountings import VerifiedMounting, verified_mountings


def _connect(pair, *, motion=None, mounting=None):
    anchor_id, moving_id = pair.part_ids
    i, j = pair.endpoint_indices
    anchor_end = partdata.record(anchor_id).connectors[i]
    moving_end = partdata.record(moving_id).connectors[j]
    build = LegoBuild()
    build.add_raw(Element(anchor_id, Color.WHITE))
    build.connect(
        Element(moving_id, Color.RED),
        moving_end,
        to=anchor_end,
        mounting=pair.parameters if mounting is None else mounting,
        motion=motion,
    )
    return build


def _place(pair, pose):
    a, b = pair.part_ids
    build = LegoBuild()
    build.add_raw(Element(a, Color.WHITE))
    build.add_raw(Element(b, Color.RED), pose)
    return build


def _position_shift(pose, shift):
    return Pose(pose.rotation, pose.translation + shift)


def _turn_about(pose, pivot, axis, angle):
    turn = Pose(rotation_about(axis, angle))
    return Pose(turn.rotation, pivot - turn.apply(pivot)).compose(pose)


@pytest.mark.parametrize("pair", verified_mountings(), ids=lambda row: row.mounting)
def test_verified_mountings_can_be_replayed_with_sdk_connections(
    pair: VerifiedMounting,
):
    assert pair in verified_mountings(pair.part_ids[0])
    assert pair in verified_mountings(pair.part_ids[1])
    build = _connect(pair, motion=pair.motion)
    assert build.placements[-1].pose == pair.pose
    assert build.is_connected()
    assert not build.colliding_placements
    if pair.motion is not None:
        assert pair.samples_per_axis is not None
        assert build.check_motion(samples=pair.samples_per_axis).passed


@pytest.mark.parametrize("part_id", ["2651", "53923"])
def test_added_mounts_round_trip_through_ldraw(part_id):
    assert part_exists(part_id)
    spec = PartSpec(part_id)
    assert spec in search_parts(name=spec.name)
    pair = verified_mountings(part_id)[0]
    model = _connect(pair)
    reloaded = from_ldraw(to_ldraw(model))
    assert (
        tuple(p.content.ldraw_id for p in reloaded.flattened_placements)
        == pair.part_ids
    )
    assert reloaded.is_connected()
    assert not reloaded.colliding_placements
    assert to_ldraw(reloaded) == to_ldraw(model)


@pytest.mark.parametrize(
    "name,shortcut,distance",
    [("retracted", "2651c01-f1", -76), ("extended", "2651c01-f2", -113)],
)
def test_hook_shortcut_pose_and_offset_rejection(name, shortcut, distance):
    pair = next(row for row in verified_mountings("2651") if row.mounting == name)
    build = _connect(pair)
    assert build.joints[0].kind is JointKind.PRISMATIC
    base, arm = build.placements
    base_raw = partdata.record("2650").ldraw_pose(base.pose)
    arm_raw = partdata.record("2651").ldraw_pose(arm.pose)
    rotation, translation = pose_to_ldraw(base_raw.inverse.compose(arm_raw))
    transform = [*translation, *rotation.flatten()]
    manifest = json.loads((DATA_DIR / "official_part_supplements.json").read_text())
    assert manifest["shortcut_poses"][shortcut]["relative_ldraw"][:3] == [
        distance,
        0,
        0,
    ]
    assert transform == pytest.approx(
        manifest["shortcut_poses"][shortcut]["relative_ldraw"]
    )

    bad_pos = _position_shift(pair.pose, Position(0, 0.15, 0))
    bad = _place(pair, bad_pos)
    assert not bad.is_connected()
    assert len(bad.colliding_placements) == 1


def test_click_lock_checks_the_indexed_tooth_positions_and_interference():
    pairs = verified_mountings("53923")
    angles = [pair.parameters.angle for pair in pairs]
    assert angles == [90 + 22.5 * i for i in range(9)]
    assert all(pair.motion is None for pair in pairs)
    for angle in [67.5, *(101.25 + 22.5 * i for i in range(8)), 292.5]:
        build = _connect(pairs[0], mounting=Mounting(angle=angle))
        assert len(build.colliding_placements) == 1
    invalid = _place(pairs[4], _position_shift(pairs[4].pose, Position(0.12, 0, 0)))
    assert not invalid.is_connected()
    assert len(invalid.colliding_placements) == 1


def test_single_finger_collider_preserves_the_axle_hole_and_insertion_stop():
    def fit(offset, angle=0):
        build = LegoBuild()
        hinge = build.add_raw(Element("53923", Color.WHITE))
        build.connect(
            Element("3705", Color.RED),
            "axle",
            to=hinge.find_connector("axle_hole"),
            mounting=Mounting(offset=offset, angle=angle),
        )
        return build

    build = fit(1.5)
    assert build.is_connected()
    assert not build.colliding_placements
    assert build.joints[0].kind is JointKind.PRISMATIC
    assert fit(1.5, angle=45).colliding_placements
    assert fit(1).colliding_placements


def test_plate_hinge_axes_stay_coincident_when_opened():
    (pair,) = verified_mountings("4275b")
    limits = MotionRange(Revolute(-90), Revolute(0))
    build = _connect(pair, motion=limits)
    (joint,) = build.joints
    assert joint.kind is JointKind.REVOLUTE
    assert build.check_motion(samples=7).passed
    opened = build.posed({joint: Revolute(-90)})
    parent, child = opened.placements
    assert connectors_mate(
        joint.parent.connector.transformed(parent.pose),
        joint.child.connector.transformed(child.pose),
    )
    assert _connect(pair, mounting=Mounting()).colliding_placements


@pytest.mark.parametrize(
    "part,axis,angle",
    [("2780", Direction.UP, 90), ("3705", Direction.DOWN, 45)],
)
def test_pin_and_axle_reject_wrong_orientation_and_off_center(part, axis, angle):
    (pair,) = verified_mountings(part)
    anchor = partdata.record(pair.part_ids[0]).connectors[pair.endpoint_indices[0]]
    wrong_orientation = _turn_about(pair.pose, anchor.position, axis.value, angle)
    assert _place(pair, wrong_orientation).colliding_placements
    off_center = _position_shift(pair.pose, Position(0.15, 0, 0))
    build = _place(pair, off_center)
    assert not build.is_connected()
    assert build.colliding_placements


def test_ball_sampling_reports_clear_and_blocked_poses():
    (pair,) = verified_mountings("22890")
    limits = MotionRange(Ball(-15, -15, -20), Ball(15, 15, 20))
    build = _connect(pair, motion=limits)
    (joint,) = build.joints
    assert joint.kind is JointKind.BALL
    assert len(limits.sample(3)) == 27
    assert build.check_motion(samples=3).passed
    wider = MotionRange(Ball(-20, -20, -30), Ball(20, 20, 30))
    build.set_motion(joint, wider)
    assert len(build.check_motion(samples=3).collisions) == 4


def test_clip_rotation_sampling_hits_stops():
    (pair,) = verified_mountings("30031")
    limits = MotionRange(Cylindrical(angle=-30), Cylindrical(angle=30))
    build = _connect(pair, motion=limits)
    (joint,) = build.joints
    assert joint.kind is JointKind.CYLINDRICAL
    assert build.check_motion(samples=7).passed
    full_twist = MotionRange(Cylindrical(angle=-180), Cylindrical(angle=180))
    build.set_motion(joint, full_twist)
    assert len(build.check_motion(samples=25).collisions) == 11


def test_plate_mates_to_the_specified_side_stud():
    (pair,) = verified_mountings("11211")
    build = _connect(pair)
    anchor, moving = build.placements
    front = moving.pose.rotation @ Direction.UP.value
    assert front == Direction.FRONT.value
    stud = partdata.record("11211").connectors[pair.endpoint_indices[0]]
    assert stud.position.x == -0.5
    assert stud.axis == front
    assert anchor.to_world(stud.position) == moving.pose.translation
