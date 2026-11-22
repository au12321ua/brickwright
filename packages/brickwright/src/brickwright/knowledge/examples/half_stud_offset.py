"""Build an offset trim course with a jumper and a measured local socket."""

import bricklayer as bl


def create_build() -> bl.LegoBuild:
    build = bl.LegoBuild()
    course = build.add(bl.Element("3004", bl.Color.TAN))
    jumper = build.stack(bl.Element("3794b", bl.Color.DARK_BLUISH_GRAY), on=course)
    trim = bl.Element("3069b", bl.Color.WHITE)
    build.connect(
        trim,
        trim.find_connector("antistud", toward=bl.Direction.RIGHT),
        to=jumper.find_connector("open_stud"),
        name="trim",
    )
    return build


def check() -> None:
    build = create_build()
    assert build.is_valid() and build.is_connected()
    # Standard trim on this course would have its origin at x=1.
    assert build.named("trim").pose.translation.x == 1.5


if __name__ == "__main__":
    check()
