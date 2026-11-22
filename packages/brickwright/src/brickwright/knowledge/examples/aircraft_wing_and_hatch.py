"""Build two wing roots on one spar and check a separate service hatch."""

import bricklayer as bl


def create_build() -> bl.LegoBuild:
    build = bl.LegoBuild()
    build.add(
        bl.Element("3795", bl.Color.LIGHT_BLUISH_GRAY), rotation=bl.Rotation.DEG_90
    )
    build.add(bl.Element("43722", bl.Color.WHITE), (-1, 0, 1 / 3), name="port wing")
    build.add(bl.Element("43723", bl.Color.WHITE), (1, 0, 1 / 3), name="starboard wing")
    fixed = build.add(bl.Element("4275b", bl.Color.LIGHT_BLUISH_GRAY), (0, 4, 1 / 3))
    build.connect(
        bl.Element("4276b", bl.Color.BLUE),
        "hinge_7_in",
        to=fixed.find_connector("hinge_7_on"),
        mounting=bl.Mounting(angle=180),
        motion=bl.MotionRange(bl.Revolute(0), bl.Revolute(60)),
        name="hatch",
    )
    return build


def check() -> None:
    build = create_build()
    assert build.is_valid() and build.is_connected()
    hatch = build.named("hatch")
    opened = build.posed({build.joint(hatch): bl.Revolute(60)})
    assert opened.named("hatch").bounding_box[1].z > hatch.bounding_box[1].z
    for name in ("port wing", "starboard wing"):
        assert opened.named(name).pose == build.named(name).pose
    assert build.check_motion(samples=7).passed


if __name__ == "__main__":
    check()
