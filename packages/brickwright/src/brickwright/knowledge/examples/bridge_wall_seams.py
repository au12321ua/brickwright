"""Tie adjacent masonry courses together with an overlapping brick."""

import bricklayer as bl


def create_build() -> bl.LegoBuild:
    build = bl.LegoBuild()
    build.add(bl.Element("3004", bl.Color.DARK_BLUISH_GRAY), name="left lower course")
    build.add(
        bl.Element("3004", bl.Color.DARK_BLUISH_GRAY),
        (2, 0, 0),
        name="right lower course",
    )
    build.add(bl.Element("3004", bl.Color.TAN), (1, 0, 1), name="staggered bond")
    return build


def check() -> None:
    build = create_build()
    assert build.is_valid() and build.is_connected()
    build.remove("staggered bond")
    assert build.is_valid() and build.num_connected_components == 2


if __name__ == "__main__":
    check()
