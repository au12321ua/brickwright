"""Reserve the keyed hole for an axle and use a round hole for a friction pin."""

import bricklayer as bl


def create_build() -> bl.LegoBuild:
    build = bl.LegoBuild()
    beam = build.add_raw(bl.Element("7229", bl.Color.DARK_BLUISH_GRAY), name="beam")
    build.connect(
        bl.Element("3705", bl.Color.BLACK),
        "axle",
        to=beam.find_connector("axle_hole"),
        name="axle",
    )
    pin = bl.Element("2780", bl.Color.BLACK)
    build.connect(
        pin,
        pin.find_connector("pin", toward=bl.Direction.LEFT),
        to=beam.find_connector("pin_hole", toward=bl.Direction.BACK),
        name="pin",
    )
    return build


def check() -> None:
    build = create_build()
    assert build.is_valid() and build.is_connected()
    assert build.joint(build.named("axle")).kind is bl.JointKind.PRISMATIC
    assert build.joint(build.named("pin")).kind is bl.JointKind.PRISMATIC


if __name__ == "__main__":
    check()
