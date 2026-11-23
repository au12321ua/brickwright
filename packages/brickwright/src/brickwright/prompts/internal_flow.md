## Build flow

Read the original condition IDs and artifact schemas through read_features and read_plan.
Use set_features and set_plan before building. Cover every original condition and identify image features, source views, planned scale, colors, independent objects and intended simplifications.
For a modular plan, use dispatch_subagents with a module ID on each task. Wait for their checked snapshots, freeze_modules, and place the named children using brickwright.modules.place_module.
Validate the entire version. Read both structure results, correct failed interfaces, and follow each diagnostic's next action. Once the structure passes, call request_aligned_review with the task camera file and directory of reference images. The independent session reads the packet and returns a feature-by-feature report.
Repair the indicated areas, validate and request a review at the same cameras. Call finalize on the current reviewed version and include any remaining items in the delivery.
