"""Move an attached fender out of the sweep of an actual rim-and-tire part."""

import bricklayer as bl

from brickwright.tools.testing import TestContext


def _test_rig(fender_position: tuple[float, float, float]) -> bl.LegoBuild:
    build = bl.LegoBuild()
    build.add(
        bl.Element("3029", bl.Color.DARK_BLUISH_GRAY), (-4, -2, 0), name="chassis"
    )
    fixed = build.add(bl.Element("3680", bl.Color.LIGHT_BLUISH_GRAY), (-1, -1, 1 / 3))
    pivot = build.connect(
        bl.Element("3679", bl.Color.LIGHT_BLUISH_GRAY),
        "turntable_pin",
        to=fixed.find_connector("turntable_socket"),
        motion=bl.MotionRange(bl.Revolute(-45), bl.Revolute(45)),
        name="steering pivot",
    )
    spacer = build.stack(bl.Element("3003", bl.Color.DARK_BLUISH_GRAY), on=pivot)
    carrier = build.stack(bl.Element("4600", bl.Color.LIGHT_BLUISH_GRAY), on=spacer)
    build.connect(
        bl.Element("4624c01", bl.Color.BLACK),
        "hinge_3_on",
        to=carrier.find_connector("hinge_3_in", toward=bl.Direction.LEFT),
        name="wheel and tire",
    )
    build.add(bl.Element("3005", bl.Color.RED), fender_position, name="fender corner")
    return build


def failing_build() -> bl.LegoBuild:
    return _test_rig((1, -2, 1 / 3))


def fixed_build() -> bl.LegoBuild:
    return _test_rig((2, -2, 1 / 3))


def check() -> None:
    build = failing_build()
    assert build.is_valid() and build.is_connected()
    pivot = build.joint(build.named("steering pivot"))
    wheel_and_fender = {build.named("wheel and tire"), build.named("fender corner")}
    report = build.check_motion(samples=5)
    assert any(
        failure.positions == ((pivot, bl.Revolute(45)),)
        and set(failure.placements) == wheel_and_fender
        for failure in report.collisions
    )
    result = TestContext(build).check_motion(samples=5)
    assert any(item.code == "motion.collision" for item in result.diagnostics)
    fixed = fixed_build()
    assert fixed.is_valid() and fixed.is_connected()
    assert TestContext(fixed).check_motion(samples=5).passed


if __name__ == "__main__":
    check()
