## Build flow

Initialize with the supplied original task file using `brickwright external init PROMPT --flow --task-json FILE`.
The following commands belong to `brickwright external`. Supply the record ID to each.

1. Read `features ID` and `plan ID` for their schemas. Copy the original condition IDs into a feature checklist and cite a view and region for additional visible features. Use `features ID --set FILE`, then `plan ID --set FILE` to record the scale, colors, intended separate objects and any agreed decorative simplifications before building.
2. When building modules, use `module init ID MODULE`, then dispatch one independently authorized builder per module. Each builder reads `module instructions CHILD --session-id SESSION`, edits its own build.py, validates and finalizes its child, and runs `module finish CHILD --session-id SESSION`. Retrieve every actual session result, run `module merge ID`, and place the checked children with `brickwright.modules.place_module`.
3. Run `validate ID` and read both the record and standard-catalog results. Resolve the specific instance and intended interface for each detached component. Use `status ID` to retrieve blockers and the next command.
4. After structure passes, run `render ID --cameras FILE`, `compare ID --reference-dir DIR`, then `review-instructions ID --parent-session-id SESSION`. Under the run's dispatch authorization, send the printed packet to a fresh image-capable reviewer with no build history. Wait for the actual image-reading session and submit its receipt and report with `review ID --set FILE`.
5. Apply the indicated repairs, validate the new version, render at the same cameras and request another independent review. Use `finalize ID` on the reviewed version. Report its delivery state and any remaining items.
