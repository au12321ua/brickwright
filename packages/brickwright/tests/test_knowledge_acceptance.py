"""Exercise the shipped corpus from the same surfaces that agents use."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
from typer.testing import CliRunner

from brickwright.cli import app
from brickwright.knowledge import search_knowledge


@pytest.mark.parametrize(
    "query,entry_id",
    [
        (
            "Stagger the seams in a chimney so both brick sides hold together",
            "bridge-wall-seams",
        ),
        (
            "Can I center a narrow strip on an even-width base with a jumper?",
            "half-stud-offset",
        ),
        (
            "The studs need to point down or face out in a vertical panel",
            "sideways-and-inverted",
        ),
        ("Fasten a pole to the ladder with a gripping bracket", "clip-and-bar"),
        ("A roof lid should swing upward", "hinged-panels"),
        ("A miniature arm should swivel in any direction", "ball-and-socket"),
        (
            "Will this shaft spin inside a round bearing or grip a keyed hole?",
            "technic-pins-and-axles",
        ),
        ("Make the roof removable as a complete module", "dock-submodels"),
        (
            "The gate is clear at each stop and catches between them",
            "check-motion-clearance",
        ),
        ("把窗饰挂在墙外侧", "sideways-and-inverted"),
        ("把玻璃窗嵌进墙面的开口，再挂上窗饰", "masonry-window-and-facade"),
        ("车轮转弯擦到车身", "steering-wheel-clearance"),
        ("前轮打满方向会蹭到挡泥板", "steering-wheel-clearance"),
        ("舱盖合得上却打不开", "aircraft-wing-and-hatch"),
        ("飞机的左右两翼怎么固定到翼梁", "aircraft-wing-and-hatch"),
        (
            "A tile is flush against the façade but comes loose",
            "diagnose-disconnected-panel",
        ),
        (
            "There are identical pin holes and it keeps picking the wrong one",
            "diagnose-ambiguous-ports",
        ),
        (
            "The keyed shaft hits the beam while I insert it",
            "diagnose-blocked-insertion",
        ),
        ("2651 为什么接不上", "diagnose-unannotated-part"),
        ("My hydraulic hose has to bend around the linkage", "flex-and-linkage-limits"),
        ("composting banana peels", None),
    ],
)
def test_corpus_search_from_cli(query, entry_id):
    external = CliRunner().invoke(app, ["knowledge", "search", query])
    assert external.exit_code == 0, external.output
    result = json.loads(external.stdout)
    assert result == search_knowledge(query).model_dump(mode="json")
    if entry_id is None:
        assert result["matches"] == []
        assert result["message"]
    else:
        assert 1 <= len(result["matches"]) <= 3
        matches = {match["id"]: match for match in result["matches"]}
        assert entry_id in matches, result
        match = matches[entry_id]
        assert match["applicability"] and match["matched_on"]


@pytest.mark.parametrize("part_id", ["2651", "10040"])
def test_indexed_part_queries_report_live_catalog_capabilities(part_id):
    from bricklayer import lookup_part

    actual = search_knowledge(f"Why won't {part_id.upper()}.DAT connect?")
    expected = lookup_part(part_id)
    assert expected.in_version
    assert any(match.id == "diagnose-unannotated-part" for match in actual.matches)
    (reported,) = actual.part_capabilities
    assert (reported.part_id, reported.version) == (expected.part_id, expected.version)
    for field in ("constructible", "geometry", "collision", "connectivity"):
        assert getattr(reported, field).status == getattr(expected, field).status
        assert (
            getattr(reported, field).reason_code == getattr(expected, field).reason_code
        )


@pytest.mark.integration
def test_installed_wheels_provide_skill_example_and_sources_outside_checkout(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build and install the wheel")
    package = Path(__file__).resolve().parents[1]
    dist = tmp_path / "dist"
    installed = tmp_path / "installed"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _run(
        [uv, "build", "--wheel", "--offline", "--no-cache", "--out-dir", str(dist)],
        cwd=package,
    )
    _run(
        [uv, "build", "--wheel", "--offline", "--no-cache", "--out-dir", str(dist)],
        cwd=package.parent / "bricklayer",
    )
    wheel = next(dist.glob("brickwright-*.whl"))
    sdk_wheel = next(dist.glob("bricklayer_sdk-*.whl"))
    with ZipFile(wheel) as content:
        assert "brickwright/knowledge/CONTENT-LICENSE.md" in content.namelist()
        assert "brickwright/skill/SKILL.md" in content.namelist()
        assert (
            content.read("brickwright/knowledge/LICENSE")
            == (package / "LICENSE").read_bytes()
        )
    _run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--no-cache",
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
            str(sdk_wheel),
        ],
        cwd=workspace,
    )
    cli = [sys.executable, "-m", "brickwright", "knowledge"]
    env = {**os.environ, "PYTHONPATH": str(installed)}
    module_sources = _run(
        [
            sys.executable,
            "-c",
            "import brickwright, bricklayer; print(brickwright.__file__); print(bricklayer.__file__)",
        ],
        cwd=workspace,
        env=env,
    )
    assert all(
        Path(source).is_relative_to(installed) for source in module_sources.splitlines()
    )
    skill = workspace / "skills" / "brickwright"
    _run(
        [
            sys.executable,
            "-m",
            "brickwright",
            "skill",
            "install",
            "--target",
            str(skill),
        ],
        cwd=workspace,
        env=env,
    )
    assert (skill / "SKILL.md").is_file()
    assert (skill / "references/api/connections.md").is_file()
    search = json.loads(
        _run(
            [*cli, "search", "Attach trim outside the glazed wall opening"],
            cwd=workspace,
            env=env,
        )
    )
    match = next(
        match
        for match in search["matches"]
        if match["id"] == "masonry-window-and-facade"
    )
    entry = json.loads(_run([*cli, "show", match["id"]], cwd=workspace, env=env))
    assert entry["steps"] and entry["expected"]
    assert entry["sources"]
    assert all(
        ref["author"] and ref["license"] and ref["adaptation"]
        for ref in entry["sources"]
    )
    article = (skill / match["path"]).read_text(encoding="utf-8")
    assert entry["expected"] in article
    assert entry["sources"][0]["license"] in article
    example = skill / "references/knowledge/examples" / entry["example"]["filename"]
    assert example.read_text(encoding="utf-8") == entry["example"]["source"]
    _run([sys.executable, str(example)], cwd=workspace, env=env)


def _run(command, *, cwd, env=None):
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout
