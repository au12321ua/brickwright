import pytest

from bricklayer import LegoBuild, Pose, from_ldraw
from bricklayer.geometry.connections import JointKind, connectors_mate
from bricklayer.ldraw.scripts.official_assemblies import rotation_error


@pytest.mark.parametrize(
    ("host_id", "child_line", "hinge"),
    [
        # Frozen LDraw 2026-10 shortcut 3135c01 (Guy Vivan, CC BY 2.0/4.0).
        ("3135", "1 16 0 -19 63 1 0 0 0 1 0 0 0 1 3136.dat", 59),
        # Shortcut 73983 (Chris Dee, CC BY 4.0).
        ("2429", "1 16 0 0 0 1 0 0 0 1 0 0 0 1 2430.dat", 39),
        # Shortcut 3324ec01 (Guy Vivan, CC BY 2.0/4.0).
        ("314e", "1 16 0 0 0 1 0 0 0 1 0 0 0 1 3324e.dat", 19),
    ],
)
def test_explicit_orientation_recovers_the_hinge_in_default_trial_budget(
    host_id, child_line, hinge
):
    host, child = from_ldraw(
        f"1 16 0 0 0 1 0 0 0 1 0 0 0 1 {host_id}.dat\n{child_line}"
    ).placements
    expected_types = {f"hinge_{hinge}_on", f"hinge_{hinge}_in"}
    assert any(
        {a.type.name, b.type.name} == expected_types and connectors_mate(a, b)
        for a in host.connectors
        for b in child.connectors
    )
    model = LegoBuild()
    first = model.add_raw(host.content, host.pose)
    candidates = model.find_connection_candidates(
        first, content=child.content, target_rotation=child.pose.rotation
    )
    obtained = [
        trial.candidate
        for trial in model.trial_connections(candidates)
        if trial.passed
        and trial.candidate.joint_kind == JointKind.REVOLUTE
        and {
            trial.candidate.parent.connector.type.name,
            trial.candidate.child.connector.type.name,
        }
        == expected_types
    ]
    assert any(
        candidate.pose is not None
        and (candidate.pose.translation - child.pose.translation).to_stud_units().norm()
        <= 0.005
        and rotation_error(child.pose, candidate.pose) <= 0.1
        for candidate in obtained
    )


def test_hinge_target_keeps_the_full_reference_tilt():
    # Crocodile jaw, LDraw 2026-10 shortcut 6026c01 (Andy Westrate, CC BY 4.0).
    host, child = from_ldraw(
        "1 16 0 0 0 1 0 0 0 1 0 0 0 1 6026.dat\n"
        "1 16 0 14 -40 1 0 0 0 0.999048 0.0436194 0 -0.0436194 0.999048 6027.dat"
    ).placements
    assert rotation_error(Pose.IDENTITY, child.pose) == pytest.approx(2.5, abs=1e-4)
    model = LegoBuild()
    first = model.add_raw(host.content, host.pose)
    candidates = model.find_connection_candidates(
        first, content=child.content, target_rotation=child.pose.rotation
    )
    (trial,) = model.trial_connections(candidates)
    assert trial.passed
    assert trial.candidate.pose is not None
    assert rotation_error(child.pose, trial.candidate.pose) <= 0.1
