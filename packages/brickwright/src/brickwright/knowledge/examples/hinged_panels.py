"""Trial a complete hatch against its frame before mounting it."""

import bricklayer as bl


def create_build() -> bl.LegoBuild:
    build = bl.LegoBuild()
    build.add(bl.plate(8, 6), rotation=bl.Rotation.DEG_90)
    build.add(bl.brick(1, 1), (2, 7, bl.PLATE))
    build.add(bl.brick(6, 1), (0, 2, bl.PLATE))
    fixed = build.add(
        bl.Element("4275b", bl.Color.LIGHT_BLUISH_GRAY),
        (2, 6, 1 + bl.PLATE),
        bl.Rotation.DEG_270,
    )

    hatch = bl.LegoBuild()
    leaf = hatch.add(bl.Element("4276b", bl.Color.BLUE))
    panel = hatch.stack(
        bl.plate(6, 4, bl.Color.BLUE),
        leaf,
        (-2, -3, 0),
        bl.Rotation.DEG_270,
        name="panel",
    )
    candidates = build.find_connection_candidates(
        fixed,
        "rotate",
        content=hatch,
        motion=bl.MotionRange(bl.Revolute(-60), bl.Revolute(0)),
        opening_toward=bl.Direction.UP,
        opening_point=panel.center,
    )
    trials = build.trial_connections(candidates, samples=7)
    for trial in trials:
        if trial.passed:
            build.place_connection(trial.candidate, name="hatch", samples=7)
            return build
    raise ValueError([str(issue) for trial in trials for issue in trial.diagnostics])


def check() -> None:
    build = create_build()
    assert build.is_valid() and build.is_connected()
    panel = build.named(("hatch", "panel"))
    joint = build.joint(build.named("hatch"))
    opened = build.posed({joint: bl.Revolute(-60)})
    assert panel.pose.rotation @ bl.Direction.UP.value == bl.Direction.UP.value
    assert opened.placements[-1].center.z > panel.center.z
    assert build.check_motion(samples=7).passed


if __name__ == "__main__":
    check()
