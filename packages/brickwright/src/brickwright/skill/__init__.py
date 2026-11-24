"""Install the BrickWright skill and its locally generated references."""

import sys
from importlib import resources
from pathlib import Path
from shlex import quote
from tempfile import TemporaryDirectory
from typing import Annotated

import typer

app = typer.Typer(help="Install the BrickWright skill.", no_args_is_help=True)


def external_instructions() -> str:
    """Point external builders to the installed skill."""
    return (
        "Install the BrickWright skill with `brickwright skill install --target DIR`.\n"
        "Choose the skill directory in your host, or use the path supplied for the task.\n"
        f"Open `DIR/SKILL.md` and run commands with `{quote(sys.executable)} -m brickwright`.\n"
        "Search for a technique, part ID, or diagnostic with `brickwright knowledge search QUERY`.\n"
        "Open the returned path under DIR, or use `brickwright knowledge show ID`.\n"
    )


def install_skill(target: Path, *, replace: bool = False) -> Path:
    """Install into an explicit directory, with opt-in replacement of skill files."""
    from bricklayer.docs import api_reference_pages

    from ..knowledge import knowledge_reference_files

    package = resources.files(__name__)
    files = {}
    for page in sorted((package / "references").iterdir(), key=lambda path: path.name):
        if page.is_file():
            files[f"references/{page.name}"] = page.read_bytes()
    files.update(
        {
            f"references/api/{filename}": markdown.encode("utf-8")
            for filename, markdown in api_reference_pages().items()
        }
    )
    files.update(knowledge_reference_files())
    root = (package / "SKILL.md").read_text(encoding="utf-8")
    files["SKILL.md"] = root.replace("{python}", quote(sys.executable)).encode("utf-8")

    target = target.expanduser().absolute()
    destinations = {target / name: content for name, content in files.items()}
    directories = {target}
    for path in destinations:
        for parent in path.parents:
            if parent == target:
                break
            directories.add(parent)
    for path in sorted(directories, key=lambda path: (len(path.parts), path)):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise FileExistsError(f"Expected a directory at {path}.")
    pending = {}
    for path, content in destinations.items():
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise FileExistsError(f"Expected a regular file at {path}.")
        if path.exists():
            if path.read_bytes() == content:
                continue
            if not replace:
                raise FileExistsError(
                    f"File differs from the skill at {path}. "
                    "Pass --replace to update the installed skill."
                )
        pending[path] = content

    if pending:
        target.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=target, prefix=".brickwright-") as directory:
            for number, (path, content) in enumerate(pending.items()):
                path.parent.mkdir(parents=True, exist_ok=True)
                staged = Path(directory) / str(number)
                staged.write_bytes(content)
                if replace:
                    staged.replace(path)
                else:
                    try:
                        path.hardlink_to(staged)
                    except FileExistsError:
                        if (
                            path.is_symlink()
                            or not path.is_file()
                            or path.read_bytes() != content
                        ):
                            raise
    return target / "SKILL.md"


@app.command()
def install(
    target: Annotated[Path, typer.Option(help="Directory to contain SKILL.md.")],
    replace: Annotated[
        bool, typer.Option(help="Replace existing skill files.")
    ] = False,
) -> None:
    """Install the skill and generate its API and knowledge pages."""
    try:
        entrypoint = install_skill(target, replace=replace)
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(entrypoint)
