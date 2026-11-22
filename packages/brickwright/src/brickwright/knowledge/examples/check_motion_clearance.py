"""Find and repair a gate collision between two clear endpoint poses."""

import bricklayer as bl

from brickwright.tools.testing import TestContext


def _gate(post_position: tuple[float, float, float]) -> bl.LegoBuild:
    build = bl.LegoBuild()
    build.add(bl.Element("3032", bl.Color.TAN), (-3, -2, 0), name="foundation")
    fixed = build.add(bl.Element("3680", bl.Color.LIGHT_BLUISH_GRAY), (-1, -1, 1 / 3))
    pivot = build.connect(
        bl.Element("3679", bl.Color.LIGHT_BLUISH_GRAY),
        "turntable_pin",
        to=fixed.find_connector("turntable_socket"),
        motion=bl.MotionRange(bl.Revolute(0), bl.Revolute(180)),
        name="pivot",
    )
    build.stack(bl.Element("3710", bl.Color.BLUE), on=pivot, name="gate")
    build.add(bl.Element("3005", bl.Color.RED), post_position, name="post")
    return build


def failing_build() -> bl.LegoBuild:
    return _gate((-1, -2, 1 / 3))


def fixed_build() -> bl.LegoBuild:
    return _gate((-2, 1, 1 / 3))


def check() -> None:
    build = failing_build()
    assert build.is_valid() and build.is_connected()
    joint = build.joint(build.named("pivot"))
    assert build.posed({joint: bl.Revolute(180)}).is_valid()
    gate_and_post = {build.named("gate"), build.named("post")}
    report = build.check_motion(samples=7)
    assert any(
        failure.positions == ((joint, bl.Revolute(90)),)
        and set(failure.placements) == gate_and_post
        for failure in report.collisions
    )
    result = TestContext(build).check_motion(samples=7)
    assert any(item.code == "motion.collision" for item in result.diagnostics)
    fixed = fixed_build()
    assert fixed.is_valid() and fixed.is_connected()
    assert TestContext(fixed).check_motion(samples=7).passed


if __name__ == "__main__":
    check()
