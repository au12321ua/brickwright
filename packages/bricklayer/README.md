# BrickLayer

**BrickLayer** is an SDK for programmatically creating and validating LEGO® builds.
Using only Python code, you can assemble models from real catalog parts, query them for structural problems (collisions,
floating parts, disconnected pieces), and export them directly to [LDraw](https://www.ldraw.org/).

## Installation

```sh
pip install bricklayer-sdk
```

## Quickstart

```python
import bricklayer as bl

model = bl.LegoBuild()
base = model.add(bl.plate(4, 4, color=bl.Color.GREEN), (0, 0, 0), name="base")
model.stack(bl.brick(2, 2, color=bl.Color.RED), on=base)

assert model.is_valid()  # no collisions, nothing floating
bl.write_ldraw(model, "tower.ldr")  # open in any LDraw viewer
```

## Official part queries

`lookup_part` and `search_official_parts` cover every top-level ID in the bundled
LDraw 2026-10 index. Each result gives its source, related IDs, and separate
statuses for visual geometry, construction, collision meshes, and endpoints.
These queries run offline with the SDK package.

```python
visual = bl.lookup_part("102525")
assert visual.geometry.available
assert visual.constructible.reason_code == "missing_collision_mesh"

hinges = bl.search_official_parts("hinge plate", constructible=True, connectivity=True)
side_studs = bl.lookup_part("11211").endpoints
```

Endpoint positions use studs on x/y and bricks on z in the part's local frame.
Each endpoint also carries its raw LDraw position in LDU, its frame transform,
and its annotation source. `PartSpec`, `Element`, and `search_parts` use the
SDK modeling catalog.

With the frozen LDraw source tree available, this command checks the bundled
index and writes a report of changed and missing entries.

```sh
python -m bricklayer.ldraw.scripts.extract_official_catalog --ldraw /path/to/ldraw --check --report catalog-report.json
```

The same command with `--compare before.json after.json` compares two index
files offline. The index includes the source tree and member checksums.

## API reference

For the full API reference, see `bricklayer.api_reference()` or run the following command:

```sh
python -m bricklayer
```

---

*Bundled part data derive from the [LDraw.org Parts Library](https://library.ldraw.org/) (CC BY 2.0/4.0),
[BrickNet](https://github.com/kulits/BrickNet) (MIT), and [Rebrickable](https://rebrickable.com/downloads/); see
[NOTICE](https://github.com/AvaLovelace1/brickwright/blob/main/packages/bricklayer/NOTICE).*

*LEGO® is a trademark of the LEGO Group, which does not sponsor, authorize, or endorse this project.*
