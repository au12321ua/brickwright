# Validate and deliver a record

Use the task's original JSON so the workflow can carry its ordered checks through to
review. Run these commands with the same records directory. Set `BRICKWRIGHT_RECORDS_DIR`
or pass `--records-dir DIR` when the task specifies one.

```sh
brickwright external init "Task prompt" --flow --task-json task.json --image target.png
brickwright external features ID
brickwright external plan ID
```

Use `--image` when the task supplies a main target image. The last two commands
show the JSON schemas. Submit a feature checklist and plan
with `features ID --set features.json` and `plan ID --set plan.json`. Preserve the
original check IDs. For additional visible features, cite a view and region.
Declare the scale, palette, intended separate objects, and any allowed decorative
simplification before building. If the design needs contracted modules, use the
[module workflow](modules.md).

Edit the printed `build.py`. Run `brickwright external validate ID` to check the
record's geometry and the same model re-read from LDraw with the standard catalog.
Examine the collision pairs, unsupported instances, and disconnected component
interfaces in the report. Use `brickwright external status ID` for the current
snapshot, diagnostics, and required next actions.

Once both structure checks pass, render the frozen model at the supplied cameras:

```sh
brickwright external render ID --cameras cameras.json
brickwright external compare ID --reference-dir DIR
```

`DIR` holds one `goal-<camera>.png` target for each camera name in `cameras.json`.
Open the [review guide](review.md) to check visual conditions against the current
render and submit a report. It also explains when a snapshot that still needs
changes may be delivered. After changing the build, rerun validation, render
the new snapshot at the same cameras, and review the new images.

Run `brickwright external finalize ID`. After it succeeds, take the record's
`out/build.ldr`. If the task requires another path or extension, copy those bytes
to the requested destination.
Verify the actual file you will submit:

```sh
brickwright external verify-delivery ID FILE
```

Submit only when this returns `ok: true`. It compares the file bytes to the
successfully finalized, verified MPD and returns the snapshot ID and SHA-256
digest. Report the delivery path, actual review mode, and any reported limitations.
