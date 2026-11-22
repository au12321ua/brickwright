"""A decorative panel touches a smooth wall without a holding connection."""

import bricklayer as bl

from brickwright.tools.testing import TestContext


def failing_build() -> bl.LegoBuild:
    build = bl.LegoBuild()
    build.add(bl.Element("3004", bl.Color.TAN), name="smooth wall")
    build.add_raw(
        bl.Element("3024", bl.Color.BLUE),
        bl.Pose(
            bl.rotation_about(bl.Direction.LEFT.value, 90),
            bl.Position(0.5, 0, 5 / 12),
        ),
        name="panel",
    )
    return build


def fixed_build() -> bl.LegoBuild:
    build = bl.LegoBuild()
    wall = build.add(bl.Element("11211", bl.Color.TAN), name="wall with side studs")
    build.connect(
        bl.Element("3024", bl.Color.BLUE),
        "antistud",
        to=wall.find_connector(
            "open_stud", toward=bl.Direction.RIGHT, facing=bl.Direction.FRONT
        ),
        name="panel",
    )
    return build


def check() -> None:
    build = failing_build()
    assert build.is_valid() and build.num_connected_components == 2
    result = TestContext(build).check_connected()
    assert any(item.code == "validation.connectivity" for item in result.diagnostics)
    fixed = fixed_build()
    assert fixed.is_valid() and TestContext(fixed).check_connected().passed


if __name__ == "__main__":
    check()
