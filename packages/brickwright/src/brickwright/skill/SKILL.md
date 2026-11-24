---
name: brickwright
description: Build or modify LEGO models with the BrickLayer Python SDK and BrickWright records. Use when choosing LDraw parts, building or repairing assemblies, checking connections and motion, reviewing renders, or delivering a validated MPD.
---

# Build a LEGO model

Use the installed SDK interpreter, `{python}`, for model code and knowledge examples.
Run the CLI with `{python} -m brickwright`. Commands below use the equivalent
`brickwright` executable on that environment's PATH.

If the task provides an original task JSON, cameras, or a verified delivery
requirement, start with the [record workflow](references/flow.md) and use `--flow`.
For a quick build, initialize a local record:

```sh
brickwright external init "Stack two 2 × 4 bricks"
```

The command prints a record ID and the editable `build.py` path. A starter
with two connected parts looks like this:

```python
import bricklayer as bl
from brickwright import TestContext


def create_build():
    build = bl.LegoBuild()
    base = build.add(bl.Element("3001", bl.Color.RED), name="base")
    build.stack(bl.Element("3001", bl.Color.BLUE), on=base, name="top")
    return build


def run_tests():
    checks = TestContext(create_build())
    checks.check_no_collisions()
    checks.check_no_floating()
    checks.check_connected(expected_components=1)
    return checks.report()
```

Run `brickwright external validate ID` after edits and inspect the reported checks
and diagnostics. For a quick build, `brickwright external finalize ID` writes
`out/build.ldr` inside the record directory. For a flow record, finish its
[render, review, and delivery steps](references/flow.md) first.

- For a connection technique or a repair, run `brickwright knowledge search QUERY`
  with a build question, part ID, or diagnostic code. Read the match's `path`
  relative to this skill, or run `brickwright knowledge show ID`.
- For a BrickLayer call or a returned field, open the relevant page in the
  [API index](references/api/index.md). With a symbol name, search that directory.
- For independently built assemblies, read [modules](references/modules.md).
- To review a frozen candidate against target images, read [review](references/review.md).
