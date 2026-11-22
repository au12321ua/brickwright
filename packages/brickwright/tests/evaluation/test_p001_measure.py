from typing import Any, cast

import bricklayer as bl
import pytest
from bricklayer import Axis, LegoBuild, Pose, Position, brick, flatten, mirror, plate

from brickwright.evaluation.p001_measure import count_placements, trace_placements


@pytest.mark.parametrize(
    "transform,joint_children",
    [
        (lambda build: build, 3),
        (lambda build: build.deepcopy(), 3),
        (flatten, 3),
        (lambda build: mirror(build, Axis.X), 3),
        (lambda build: build.posed({}), 0),
    ],
)
def test_counts_repeated_subassemblies_and_avoids_double_counting(
    transform, joint_children
):
    with trace_placements():
        section = LegoBuild()
        bottom = section.add(plate(1, 1))
        section.connect(plate(1, 1), "antistud", to=bottom.find_connector("stud"))
        model = LegoBuild()
        base = model.add(brick(1, 1))
        model.connect(
            section,
            bottom.find_connector("antistud"),
            to=base.find_connector("stud"),
        )
        model.add(section, (4, 0, 0))
        transformed = transform(model)
    counts = count_placements(transformed)
    assert counts["pieces"] == 5
    assert counts["connector_placed_pieces"] == 3
    assert counts["direct_connector_pieces"] == 2
    assert counts["subassembly_connector_pieces"] == 2
    assert counts["live_joint_child_pieces"] == joint_children


def test_excludes_grid_mating_failed_trials_and_removed_placements():
    with trace_placements():
        model = LegoBuild()
        base = model.add(brick(1, 1))
        model.stack(plate(1, 1), on=base)
        other = model.add(brick(1, 1), (2, 0, 0))
        target = other.find_connector("stud")
        with pytest.raises(ValueError):
            model.connect(plate(1, 1), "stud", to=target)
        trial = model.copy()
        trial.connect(plate(1, 1), "antistud", to=target)
        removed = model.connect(plate(1, 1), "antistud", to=target)
        model.remove(removed)
    assert model.num_connected_components == 2
    assert count_placements(model)["pieces"] == 3
    assert count_placements(model)["connector_placed_pieces"] == 0


def test_connector_origin_survives_removing_parent_or_using_geometric_target():
    with trace_placements():
        model = LegoBuild()
        base = model.add(brick(1, 1))
        model.connect(plate(1, 1), "antistud", to=base.find_connector("stud"))
        target = base.find_connector("stud").transformed(
            Pose(translation=Position(4, 0, 0))
        )
        model.connect(plate(1, 1), "antistud", to=target)
        model.remove(base)
        other = LegoBuild()
        other.extend(model)
    counts = count_placements(other)
    assert counts["pieces"] == counts["connector_placed_pieces"] == 2
    assert counts["live_joint_child_pieces"] == 0


def test_checked_placement_hook_does_not_depend_on_connect(monkeypatch):
    def place_connection(model, content):
        return model.add(content)

    monkeypatch.setattr(LegoBuild, "place_connection", place_connection, raising=False)
    with trace_placements():
        model = LegoBuild()
        cast(Any, model).place_connection(plate(1, 1))
    assert count_placements(model)["connector_placed_pieces"] == 1


@pytest.mark.skipif(
    not hasattr(LegoBuild, "place_connection"), reason="SDK before P-001"
)
@pytest.mark.parametrize("subassembly", [False, True])
def test_checked_trials_count_only_after_committing(subassembly):
    with trace_placements():
        model = LegoBuild()
        checked_model = cast(Any, model)
        model.add(bl.Element("4275b", bl.Color.WHITE), name="frame")
        hinge = bl.Element("4276b", bl.Color.WHITE)
        content = hinge
        if subassembly:
            door = LegoBuild()
            leaf = door.add(hinge)
            door.stack(plate(2, 1), on=leaf)
            content = door
        candidates = checked_model.find_connection_candidates(
            "frame",
            "rotate",
            content=content,
            facing=bl.Direction.BACK,
            mountings=(bl.Mounting(angle=90),),
            limit=1,
        )
        (trial,) = checked_model.trial_connections(candidates)
        assert trial.passed
        assert count_placements(model)["connector_placed_pieces"] == 0
        placement = checked_model.place_connection(trial.candidate)
        counts = count_placements(model)
        assert counts["connector_placed_pieces"] == (2 if subassembly else 1)
        assert counts["live_joint_child_pieces"] == 1
        model.remove(placement)
        assert count_placements(model)["connector_placed_pieces"] == 0
