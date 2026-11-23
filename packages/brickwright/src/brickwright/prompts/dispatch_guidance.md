Use `dispatch_subagents` for independent work. Each scratch worker returns text for you to integrate.
When the current plan declares modules, set a task's `module` to a module ID. Give the worker its
building brief. It edits and validates its own module record and returns the finalized snapshot.
You can place those snapshots with `place_module(build, module_id)` in the parent build.

Per task, you may set `tier` to `fast`, `balanced`, or `smart`. Omit it to use your own model.
