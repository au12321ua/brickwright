"""Anchor the fixed leaf and sample a finger hinge from flat to raised."""

import bricklayer as bl


def create_build() -> bl.LegoBuild:
    build = bl.LegoBuild()
    support = build.add(bl.Element("3023", bl.Color.DARK_BLUISH_GRAY))
    fixed = build.stack(bl.Element("4275b", bl.Color.LIGHT_BLUISH_GRAY), on=support)
    build.connect(
        bl.Element("4276b", bl.Color.BLUE),
        "hinge_7_in",
        to=fixed.find_connector("hinge_7_on"),
        mounting=bl.Mounting(angle=180),
        motion=bl.MotionRange(bl.Revolute(0), bl.Revolute(60)),
        name="leaf",
    )
    return build


def check() -> None:
    build = create_build()
    assert build.is_valid() and build.is_connected()
    leaf = build.named("leaf")
    joint = build.joint(leaf)
    assert joint.kind is bl.JointKind.REVOLUTE
    assert build.posed({joint: bl.Revolute(60)}).named("leaf").pose != leaf.pose
    report = build.check_motion(samples=7)
    assert report.samples_checked == 7 and report.passed


if __name__ == "__main__":
    check()
