"""Trial the wrong and correct clocking of a cross axle without changing the build."""

import bricklayer as bl


def check() -> None:
    build = bl.LegoBuild()
    beam = build.add_raw(bl.Element("7229", bl.Color.DARK_BLUISH_GRAY), name="beam")
    axle = bl.Element("3705", bl.Color.BLACK)

    candidates = build.find_connection_candidates(
        beam,
        "slide",
        content=axle,
        parent_type="axle_hole",
        child_type="axle",
        mountings=(bl.Mounting(angle=45),),
    )
    (blocked,) = build.trial_connections(candidates, limit=1)
    assert any(
        diagnostic.code == "assembly.collision" for diagnostic in blocked.diagnostics
    )
    assert build.placements == (beam,) and not build.joints

    candidates = build.find_connection_candidates(
        beam,
        "slide",
        content=axle,
        parent_type="axle_hole",
        child_type="axle",
        mountings=(bl.Mounting(angle=0),),
    )
    (clear,) = build.trial_connections(candidates, limit=1)
    assert clear.passed
    build.place_connection(clear.candidate, name="axle")
    assert build.is_valid() and build.is_connected()


if __name__ == "__main__":
    check()
