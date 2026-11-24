# Review the current render

Use [the record workflow](flow.md) to validate and render the active snapshot.
The reference directory must contain one `goal-<camera>.png` for each camera name
in the supplied camera file, such as `goal-top.png` for camera `top`. A task input
directory may already use these names. Then run
`brickwright external compare ID --reference-dir DIR`.

Request the packet and report schema:

```sh
brickwright external review-instructions ID --parent-session-id SESSION
```

Use either review mode:

- **Self-review.** Open the packet's target and candidate images yourself. Set
  `reviewer.dispatch` to `self`, with both session fields set to the parent
  session ID used to create the packet.
- **Independent review.** If you can dispatch an image-capable subagent, give it
  the frozen packet and instructions in a fresh session. Use the actual parent and
  reviewer session IDs and the matching subagent dispatch value from the schema.

Open every required image before claiming it in `reviewer.image_reads`. Use the
overlays to locate differences, then judge the task's conditions and visible
features from the actual images. For hidden connections and other conditions
requiring physical evidence, consult the same snapshot's structural checks.
Use `run_tests()` to measure task conditions that need a custom check. For
condition `C01`, name its check `C01` or use a prefix such as
`C01_axle_clearance` so the review packet can associate the result with the
condition. Record the specific repairs needed and any unresolved items. Preserve the packet's
round ID and input digest in the report.

Submit with `brickwright external review ID --set report.json`. If required images
are unavailable, submit a failed attempt with the round binding and failure reason
shown in the packet. When repairs change the model, validate and review the new
snapshot before finalizing.

Report the verdict the images support. A `needs_changes` verdict blocks
`finalize`. The record allows a fixed number of accepted review rounds across all
snapshots, two unless `external init --review-rounds N` set another number. When
the round that uses up this budget reviews the current snapshot, returns
`needs_changes` and lists each remaining item, `finalize` accepts that snapshot.
Delivery then reports `review_outcome: rounds_exhausted`. Report those remaining
items with the delivery. Do not change the verdict to `passed` to unblock delivery.
