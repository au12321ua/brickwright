# Build with checked modules

Use this workflow when subassemblies need independently checked source files and
declared mating interfaces. For nested submodels within one `build.py`, see
[placement](api/placement.md).

In the parent [plan](flow.md), define each module's pose, occupied region and
interfaces. Create each child using its planned ID:

```sh
brickwright external module init ID MODULE
brickwright external module instructions CHILD --session-id SESSION
```

Use the child record ID printed at initialization. In its `build.py`, return the
module in its own local coordinates. Leave the declared endpoints clear for
mating geometry. Run `brickwright external validate CHILD` and
`brickwright external finalize CHILD`, then record the session with
`brickwright external module finish CHILD --session-id SESSION`.

Check `brickwright external module status ID` and run
`brickwright external module merge ID` after all planned modules pass.
Place each sealed module once in the parent in `create_build()`:

```python
import bricklayer as bl
from brickwright.modules import place_module


def create_build():
    assembly = bl.LegoBuild()
    place_module(assembly, "frame")
    place_module(assembly, "door")
    return assembly
```

Use the actual planned IDs. The merged snapshot supplies the world transforms.
Validate the parent to check the resolved interfaces and any prescribed motion
with all modules present. After local repairs, finalize the changed child, rerun
the merge, and validate the parent again.
