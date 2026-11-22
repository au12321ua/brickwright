"""Fit a horizontal rail to a clip plate at the edge of a deck."""

import bricklayer as bl


def create_build() -> bl.LegoBuild:
    build = bl.LegoBuild()
    deck = build.add(bl.Element("3020", bl.Color.TAN))
    bracket = build.stack(
        bl.Element("60470b", bl.Color.LIGHT_BLUISH_GRAY), on=deck, offset=(1, 0, 0)
    )
    build.connect(
        bl.Element("30374", bl.Color.DARK_BLUISH_GRAY),
        "bar",
        to=bracket.find_connector("clip", toward=bl.Direction.LEFT),
        name="rail",
    )
    return build


def check() -> None:
    build = create_build()
    assert build.is_valid() and build.is_connected()
    rail = build.named("rail")
    rail.find_connector("bar", facing=bl.Direction.LEFT)
    assert build.joint(rail).kind is bl.JointKind.CYLINDRICAL


if __name__ == "__main__":
    check()
