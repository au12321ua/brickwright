"""Check the copyable skill and the paths builders get from knowledge search."""

import json
import re
import sys
from pathlib import Path
from shlex import quote

from bricklayer.docs.utils import code_blocks
from typer.testing import CliRunner

from brickwright.cli import app
from brickwright.knowledge import read_knowledge, search_knowledge


def test_skill_installs_a_complete_linked_tree(tmp_path):
    target = tmp_path / "skills" / "brickwright"
    runner = CliRunner()
    result = runner.invoke(app, ["skill", "install", "--target", str(target)])
    assert result.exit_code == 0, result.output
    assert str(target / "SKILL.md") in result.output

    root = (target / "SKILL.md").read_text(encoding="utf-8")
    assert root.startswith("---\nname: brickwright\ndescription:")
    assert f"{quote(sys.executable)} -m brickwright" in root
    assert "find_connection_candidates(" not in root
    (starter,) = [
        block.code for block in code_blocks(root) if block.language == "python"
    ]
    namespace = {}
    exec(starter, namespace)
    model = namespace["create_build"]()
    assert model.brick_count == 2
    assert model.is_valid()
    assert model.is_connected()
    checks = namespace["run_tests"]()
    assert checks and all(check.passed for check in checks)

    pages = tuple(target.rglob("*.md"))
    for page in pages:
        for destination in re.findall(
            r"\]\(([^)\s]+)\)", page.read_text(encoding="utf-8")
        ):
            path = destination.split("#", 1)[0]
            if path and "://" not in path:
                assert (page.parent / path).is_file(), (page, destination)

    match = next(
        item
        for item in search_knowledge("舱门翻开").matches
        if item.id == "hinged-panels"
    )
    entry = read_knowledge(match.id)
    assert entry.example is not None
    page = (target / match.path).read_text(encoding="utf-8")
    assert entry.expected in page
    assert entry.sources[0].attribution_url in page
    example = target / "references/knowledge/examples" / entry.example.filename
    assert example.read_text(encoding="utf-8") == entry.example.source
    assert (target / "references/knowledge/CONTENT-LICENSE.md").is_file()
    assert (target / "references/knowledge/LICENSE").is_file()
    assert (
        runner.invoke(app, ["skill", "install", "--target", str(target)]).exit_code == 0
    )


def test_skill_quick_start_creates_and_exports_a_valid_record(tmp_path):
    from brickwright.skill import install_skill

    runner = CliRunner()
    skill = install_skill(tmp_path / "brickwright")
    (starter,) = [
        block.code
        for block in code_blocks(skill.read_text(encoding="utf-8"))
        if block.language == "python"
    ]
    records = tmp_path / "records"
    init = runner.invoke(
        app,
        ["external", "init", "Stack two 2 x 4 bricks", "--records-dir", str(records)],
    )
    assert init.exit_code == 0, init.output
    record = json.loads(init.stdout)
    build = Path(record["build_path"])
    build.write_text(starter, encoding="utf-8")
    for command in ("validate", "finalize"):
        result = runner.invoke(
            app,
            ["external", command, record["record_id"], "--records-dir", str(records)],
        )
        assert result.exit_code == 0, result.output
    exported = build.parent / "out/build.ldr"
    assert exported.read_text(encoding="utf-8").count("3001.dat") == 2


def test_skill_install_preserves_existing_files_on_conflict(tmp_path):
    target = tmp_path / "brickwright"
    conflict = target / "references/flow.md"
    conflict.parent.mkdir(parents=True)
    conflict.write_text("Host edits", encoding="utf-8")
    result = CliRunner().invoke(app, ["skill", "install", "--target", str(target)])
    assert result.exit_code == 1, result.output
    assert str(conflict) in result.output
    assert conflict.read_text(encoding="utf-8") == "Host edits"
    assert not (target / "SKILL.md").exists()


def test_skill_install_can_replace_previous_generated_files(tmp_path):
    target = tmp_path / "brickwright"
    target.mkdir()
    entrypoint = target / "SKILL.md"
    entrypoint.write_text("Previous version", encoding="utf-8")
    unrelated = target / "host-note.md"
    unrelated.write_text("Host configuration", encoding="utf-8")
    result = CliRunner().invoke(
        app, ["skill", "install", "--target", str(target), "--replace"]
    )
    assert result.exit_code == 0, result.output
    assert "name: brickwright" in entrypoint.read_text(encoding="utf-8")
    assert unrelated.read_text(encoding="utf-8") == "Host configuration"


def test_skill_install_rejects_links_into_other_host_directories(tmp_path):
    target = tmp_path / "brickwright"
    target.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (target / "references").symlink_to(unrelated, target_is_directory=True)
    result = CliRunner().invoke(app, ["skill", "install", "--target", str(target)])
    assert result.exit_code == 1, result.output
    assert str(target / "references") in result.output
    assert not list(unrelated.iterdir())
    assert not (target / "SKILL.md").exists()


def test_external_instructions_are_an_entry_point_to_the_skill():
    result = CliRunner().invoke(app, ["external", "instructions", "--flow"])
    assert result.exit_code == 0, result.output
    assert "skill install --target" in result.stdout
    assert "SKILL.md" in result.stdout
    assert f"{quote(sys.executable)} -m brickwright" in result.stdout
    assert "brickwright knowledge search" in result.stdout
    assert "brickwright knowledge show" in result.stdout
    assert len(result.stdout.splitlines()) < 30
