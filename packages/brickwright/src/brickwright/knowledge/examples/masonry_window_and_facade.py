"""Frame a small window and hang trim on the outside of the right pier."""

import bricklayer as bl


def create_build() -> bl.LegoBuild:
    build = bl.LegoBuild()
    sill = build.add(bl.Element("3795", bl.Color.LIGHT_BLUISH_GRAY), name="sill")
    build.stack(bl.Element("2420", bl.Color.TAN), on=sill, name="corner return")
    frame = build.add(
        bl.Element("60593", bl.Color.WHITE), (1, 0, 2 / 3), name="window frame"
    )
    for height in (2 / 3, 5 / 3, 8 / 3):
        build.add(bl.Element("3005", bl.Color.TAN), (0, 0, height))
        build.add(bl.Element("3005", bl.Color.TAN), (0, 1, height))

    build.add(bl.Element("3004", bl.Color.TAN), (3, 0, 2 / 3))
    facade = build.add(bl.Element("11211", bl.Color.TAN), (3, 0, 5 / 3))
    build.add(bl.Element("3004", bl.Color.TAN), (3, 0, 8 / 3))
    build.add(bl.Element("3710", bl.Color.TAN), (0, 0, 11 / 3), name="header")
    build.connect(
        bl.Element("60602", bl.Color.TRANS_CLEAR),
        "pane_tab",
        to=frame.find_connector("frame_slot"),
        name="pane",
    )
    trim = bl.Element("3069b", bl.Color.DARK_GREEN)
    build.connect(
        trim,
        trim.find_connector("antistud", toward=bl.Direction.RIGHT),
        to=facade.find_connector(
            "open_stud", toward=bl.Direction.RIGHT, facing=bl.Direction.FRONT
        ),
        name="outside trim",
    )
    return build


def check() -> None:
    build = create_build()
    assert build.is_valid() and build.is_connected()
    assert build.named("outside trim").bounding_box[1].y == 0
    assert build.joint(build.named("pane")).parent.path == (
        build.named("window frame"),
    )


if __name__ == "__main__":
    check()
