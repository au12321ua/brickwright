"""Reproduce an unqualified pin-hole lookup, then select the outer hole."""

import bricklayer as bl
from bricklayer.diagnostics import DiagnosticError


def _beam_and_pin() -> tuple[bl.LegoBuild, bl.Placement[bl.Element], bl.Element]:
    build = bl.LegoBuild()
    beam = build.add_raw(bl.Element("7229", bl.Color.DARK_BLUISH_GRAY), name="beam")
    pin = bl.Element("2780", bl.Color.BLACK)
    return build, beam, pin


def failing_build() -> bl.LegoBuild:
    build, beam, pin = _beam_and_pin()
    build.connect(
        pin,
        pin.find_connector("pin", toward=bl.Direction.LEFT),
        to=beam.find_connector("pin_hole"),
    )
    return build


def fixed_build() -> bl.LegoBuild:
    build, beam, pin = _beam_and_pin()
    build.connect(
        pin,
        pin.find_connector("pin", toward=bl.Direction.LEFT),
        to=beam.find_connector("pin_hole", toward=bl.Direction.BACK),
        name="outer pin",
    )
    return build


def check() -> None:
    try:
        failing_build()
    except DiagnosticError as error:
        assert error.code == "connector.ambiguous"
    else:
        raise AssertionError("both outer pin holes match the unqualified selector")
    fixed = fixed_build()
    assert fixed.is_valid() and fixed.is_connected()
    assert fixed.joint(fixed.named("outer pin")).parent.connector.position.y == 1


if __name__ == "__main__":
    check()
