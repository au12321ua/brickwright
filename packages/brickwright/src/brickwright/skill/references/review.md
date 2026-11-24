# Review the current render

Use [the record workflow](flow.md) to validate and render the active snapshot.
The reference directory must contain one `goal-<camera>.png` for each camera name
in the supplied camera file, such as `goal-top.png` for camera `top`. A task input
directory may already use these names. Then run
`brickwright external compare ID --reference-dir DIR`.

Prepare a review round:

```sh
brickwright external review-instructions ID
```

The result names the frozen `packet_dir`, an `instructions_path` and a
`report_path`. The report is a template that already holds the round ID, the
input digest, the reviewer session fields and every original condition and
feature to judge. Its `next` step is to fill in that template and submit it.

Use either review mode:

- **Self-review.** Open the packet's target and candidate images yourself. Keep
  `reviewer.dispatch` as `self` and both prefilled session fields.
- **Independent review.** If you can dispatch an image-capable subagent, give it
  the instructions file and the packet in a fresh session. Set
  `reviewer.dispatch` to the matching subagent value and `reviewer.session_id` to
  the reviewer's own session ID.

Open every required image before listing it in `reviewer.image_reads`. Use the
overlays to locate differences, then judge the task's conditions and visible
features from the actual images. For hidden connections and other conditions
requiring physical evidence, consult the same snapshot's structural checks.
Use `run_tests()` to measure task conditions that need a custom check. For
condition `C01`, name its check `C01` or use a prefix such as
`C01_axle_clearance` so the review packet can associate the result with the
condition. Record the specific repairs needed and any unresolved items.

The original conditions are a minimum. Also compare colour regions, markings
and proportions. Mark a finding `required` when it changes a view's outline,
colour layout or proportions and you can still repair it with the allowed
parts. Leave it unrequired only when the parts, piece limit or connections
prevent a closer match, and say why in its `repair`. A required finding blocks
`passed`, so the next round goes to the repair. Once the rounds are used up,
the remaining items are delivered as described below.

Submit with `brickwright external review ID --set REPORT`. If required images
are unavailable, replace the report with `schema_version`, `round_id`,
`input_sha256` and `failure_reason`; this archives a failed attempt that does not
count as a round, and `next` prepares a new one.

## When a submission is rejected or the model changes

- **The report is incomplete or invalid.** For example, it omits condition C01.
  Nothing is stored and `round_open` is true. The diagnostic names the fields and
  the missing or unknown IDs. Correct the same report file and run the returned
  `next` step, which submits it to the same round.
- **The model changed after the round was prepared.** A report for the old
  snapshot is archived as `stale`, exits 1 and does not count. Follow `next`:
  validate the current build, render and compare the new snapshot at the same
  cameras, and review the new images.

Each accepted report states `receipt`, whether it was `counted`, the `rounds`
used and remaining, and whether the snapshot is `deliverable`.

Report the verdict the images support. A `needs_changes` verdict blocks
`finalize`. The record allows a fixed number of accepted review rounds across all
snapshots, two unless `external init --review-rounds N` set another number. When
the round that uses up this budget reviews the current snapshot, returns
`needs_changes` and lists each remaining item, `finalize` accepts that snapshot.
Delivery then reports `review_outcome: rounds_exhausted`. Report those remaining
items with the delivery. Do not change the verdict to `passed` to unblock delivery.
Delivery still requires a successful `finalize` and a submitted file that matches
the verified snapshot byte for byte.
