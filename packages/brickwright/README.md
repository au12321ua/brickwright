# BrickWright

Build LEGO® models with a host coding agent and the BrickLayer Python SDK. BrickWright stores the editable model, validation results, camera views, reviews and finalized LDraw export.

## Install the fork and skill

From the repository root, run:

```sh
uv sync --locked
uv run brickwright skill install --target .agents/skills/brickwright
```

For Claude Code, use `.claude/skills/brickwright` as the target. Open the project in your host agent and ask it to build from your text or reference images using the BrickWright skill. The installed `SKILL.md` includes the SDK interpreter and a working two-brick example. Set `LDRAW_LIBRARY_PATH` to your LDraw parts library to render the model.

## Find a technique

```sh
uv run brickwright knowledge search 'a hatch that hits the surrounding bodywork'
uv run brickwright knowledge show hinged-panels
```

Matches include a path relative to the installed skill. Open a technique, example, or API topic when the build needs it.

## Accept a demo submission

Use a flow record with the original task, validate and review the current model, then run `brickwright external finalize ID`. Copy the exported `out/build.ldr` to the submission path. The receiver checks the submitted file against the verified snapshot and the current successful finalization:

```sh
uv run brickwright external verify-delivery ID model.mpd
```

The command exits successfully on a match and returns the snapshot ID, MPD digest and review method. Records default to `./records`. Use `BRICKWRIGHT_RECORDS_DIR` or `--records-dir` for another location. The single-task example includes the verifier used by Harbor. [OMR36 demo](../../demo/omr36/README.md)

*LEGO® is a trademark of the LEGO Group, which does not sponsor, authorize, or endorse this project.*
