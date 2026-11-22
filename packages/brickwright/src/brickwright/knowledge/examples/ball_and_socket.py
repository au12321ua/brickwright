"""Attach an angled lug and test a small single-axis swing in its ball joint."""

import bricklayer as bl


def create_build() -> bl.LegoBuild:
    build = bl.LegoBuild()
    base = build.add(bl.Element("3020", bl.Color.TAN))
    socket = build.stack(
        bl.Element("14418", bl.Color.LIGHT_BLUISH_GRAY), on=base, offset=(2, 0, 0)
    )
    build.connect(
        bl.Element("22890", bl.Color.BLUE),
        "towball",
        to=socket.find_connector("towball_socket"),
        mounting=bl.Mounting(angle=180),
        motion=bl.MotionRange(bl.Ball(-5, 0, 0), bl.Ball(5, 0, 0)),
        name="lug",
    )
    return build


def check() -> None:
    build = create_build()
    assert build.is_valid() and build.is_connected()
    lug = build.named("lug")
    joint = build.joint(lug)
    assert joint.kind is bl.JointKind.BALL
    swung = build.posed({joint: bl.Ball(5, 0, 0)}).named("lug")
    assert swung.pose.rotation != lug.pose.rotation
    assert build.check_motion(samples=5).passed


if __name__ == "__main__":
    check()
