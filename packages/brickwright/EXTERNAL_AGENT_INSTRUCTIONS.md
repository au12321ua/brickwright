# Building with BrickWright

Use the installed BrickWright skill in the current host project. From this fork's checkout, install it with:

```sh
uv sync --locked
uv run brickwright skill install --target .agents/skills/brickwright
```

Open the installed `SKILL.md` for environment paths, the starter build and the reference relevant to your task. A dataset with a flow task supplies its original `task.json` and its expected submission path. Finalize that record and verify the submitted file using `brickwright external verify-delivery ID FILE`. [Installation and usage](README.md)
