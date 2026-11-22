"""Consult the current catalog before choosing an alternate hinge design."""

import bricklayer as bl
from bricklayer.diagnostics import DiagnosticError


def fixed_build() -> bl.LegoBuild:
    build = bl.LegoBuild()
    leaf = build.add(bl.Element("4275b", bl.Color.DARK_BLUISH_GRAY), name="fixed leaf")
    build.connect(
        bl.Element("4276b", bl.Color.BLUE),
        "hinge_7_in",
        to=leaf.find_connector("hinge_7_on"),
        mounting=bl.Mounting(angle=180),
        motion=bl.MotionRange(bl.Revolute(0), bl.Revolute(60)),
        name="moving leaf",
    )
    return build


def check() -> None:
    hook = bl.lookup_part("2651")
    base = bl.lookup_part("2650")
    assert hook.constructible.available and base.constructible.available
    assert base.connectivity.available
    # Connector support may change with catalog releases. Discuss the active result.
    assert hook.connectivity.available == bool(hook.endpoints)

    missing = bl.lookup_part("10040")
    assert missing.constructible.available and missing.collision.available
    assert missing.connectivity.reason_code == "missing_connector_annotations"
    assert not missing.endpoints
    cover = bl.Element("10040", bl.Color.DARK_BLUISH_GRAY)
    try:
        cover.find_connector("hinge_7_in")
    except DiagnosticError as error:
        assert error.code == "catalog.missing_data"
    else:
        raise AssertionError("the motor cover has no annotated hinge endpoint")

    fixed = fixed_build()
    assert fixed.is_valid() and fixed.is_connected()
    assert fixed.check_motion(samples=7).passed


if __name__ == "__main__":
    check()
