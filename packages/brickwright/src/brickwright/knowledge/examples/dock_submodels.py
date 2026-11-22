"""Mount two copies of a removable panel by their free local connectors."""

import bricklayer as bl


def create_build() -> bl.LegoBuild:
    module = bl.LegoBuild()
    root = module.add(bl.Element("3024", bl.Color.BLUE), name="root")
    module.stack(bl.Element("3070b", bl.Color.WHITE), on=root, name="finish")

    build = bl.LegoBuild()
    rail = build.add(bl.Element("11211", bl.Color.LIGHT_BLUISH_GRAY), name="rail")
    for name, edge in (("left", bl.Direction.LEFT), ("right", bl.Direction.RIGHT)):
        build.connect(
            module,
            "antistud",
            to=rail.find_connector("open_stud", toward=edge, facing=bl.Direction.FRONT),
            name=name,
        )
    return build


def check() -> None:
    build = create_build()
    assert build.is_valid() and build.is_connected()
    assert len(build.flattened_placements) == 5
    assert build.named("left").content is build.named("right").content
    assert build.named("left").pose != build.named("right").pose


if __name__ == "__main__":
    check()
