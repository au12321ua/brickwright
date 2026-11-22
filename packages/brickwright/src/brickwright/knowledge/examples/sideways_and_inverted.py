"""Use two side studs to mount a sideways plate and an upside-down brick."""

import bricklayer as bl


def create_build() -> bl.LegoBuild:
    build = bl.LegoBuild()
    core = build.add(bl.Element("11211", bl.Color.LIGHT_BLUISH_GRAY), name="core")
    build.connect(
        bl.Element("3024", bl.Color.BLUE),
        "antistud",
        to=core.find_connector(
            "open_stud", toward=bl.Direction.RIGHT, facing=bl.Direction.FRONT
        ),
        name="side plate",
    )
    headlight = bl.Element("4070", bl.Color.DARK_BLUISH_GRAY)
    build.connect(
        headlight,
        headlight.find_connector("antistud", facing=bl.Direction.FRONT),
        to=core.find_connector(
            "open_stud", toward=bl.Direction.LEFT, facing=bl.Direction.FRONT
        ),
        mounting=bl.Mounting(angle=180),
        name="inverted headlight",
    )
    return build


def check() -> None:
    build = create_build()
    assert build.is_valid() and build.is_connected()
    build.named("side plate").find_connector("stud", facing=bl.Direction.FRONT)
    build.named("inverted headlight").find_connector("stud", facing=bl.Direction.DOWN)


if __name__ == "__main__":
    check()
