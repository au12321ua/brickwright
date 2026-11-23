import pytest

from bricklayer import LegoBuild, Pose, from_ldraw
from bricklayer.geometry.connections import connectors_mate
from bricklayer.ldraw import partdata
from bricklayer.ldraw.conversion import LDrawVec, pose_to_ldraw


@pytest.mark.parametrize(
    ("rim", "tyre", "true_z", "false_z", "family"),
    [
        ("7039", "u9131", -2, -2.33335, 5),
        ("7039", "3483", -2, -2.33335, 5),
        ("56904", "56898", 0, -2, 21),
        ("56904", "30699", 0, -2, 21),
    ],
)
def test_tyre_acquires_the_rim_bead_at_the_correct_depth(
    rim: str, tyre: str, true_z: float, false_z: float, family: int
) -> None:
    def load(depth: float) -> LegoBuild:
        return from_ldraw(
            f"1 4 0 0 0 1 0 0 0 1 0 0 0 1 {rim}.dat\n"
            f"1 0 0 0 {depth} 1 0 0 0 1 0 0 0 1 {tyre}.dat\n"
        )

    reference = load(true_z)
    host, child = reference.flattened_placements
    assert not reference.colliding_placements
    assert any(
        a.type.name == f"fixed_{family}_in"
        and b.type.name == f"fixed_{family}_on"
        and connectors_mate(a, b)
        for a in host.connectors
        for b in child.connectors
    )
    assert not load(false_z).is_connected()

    build = LegoBuild()
    host = build.add_raw(host.content, host.pose)
    candidates = build.find_connection_candidates(
        host, content=child.content, target_rotation=child.pose.rotation
    )
    for trial in build.trial_connections(candidates):
        if trial.passed:
            added = build.place_connection(trial.candidate)
            pose = partdata.record(tyre).ldraw_pose(added.pose)
            _, translation = pose_to_ldraw(pose)
            assert translation.tolist() == pytest.approx([0, 0, true_z], abs=0.1)
            assert pose.rotation == Pose.IDENTITY.rotation
            break
    else:
        pytest.fail("The correct bead seat did not pass the default trial budget")


def test_pose_rule_compares_the_full_rotation() -> None:
    from bricklayer.geometry.linalg import rotation_about
    from bricklayer.geometry.spatial import Direction
    from bricklayer.ldraw.scripts.official_assemblies import rotation_error

    pose = Pose(
        rotation_about(Direction.UP.value, 0.2), LDrawVec(0, 0, 0).to_position()
    )
    assert rotation_error(Pose.IDENTITY, pose) == pytest.approx(0.2)
