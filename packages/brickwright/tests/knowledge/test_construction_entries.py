"""Offline checks of the shipped construction guidance and its examples."""

import ast
import json
import re
import runpy
from importlib.resources import as_file, files

import pytest
from bricklayer import part_exists

CORPUS = files("brickwright").joinpath("knowledge")
REQUIRED_EXAMPLES = {
    "aircraft-wing-and-hatch",
    "ball-and-socket",
    "bridge-wall-seams",
    "check-motion-clearance",
    "clip-and-bar",
    "diagnose-ambiguous-ports",
    "diagnose-blocked-insertion",
    "diagnose-disconnected-panel",
    "diagnose-unannotated-part",
    "dock-submodels",
    "half-stud-offset",
    "hinged-panels",
    "masonry-window-and-facade",
    "sideways-and-inverted",
    "steering-wheel-clearance",
    "technic-pins-and-axles",
}
NOVA_URL = (
    "https://github.com/anteloc/ldraw-nova/blob/"
    "d1e93d73f6ffe6955caa90953a98a0c90fbd5c5c/"
)


def test_topic_coverage_and_documentation_licenses():
    entries = {
        item.name.removesuffix(".json"): json.loads(item.read_text(encoding="utf-8"))
        for item in CORPUS.joinpath("entries").iterdir()
        if item.name.endswith(".json")
    }
    assert REQUIRED_EXAMPLES <= entries.keys()
    assert not entries["flex-and-linkage-limits"]["example"]
    for entry_id, entry in entries.items():
        assert entry_id == entry["id"]
        assert re.fullmatch(r"[a-z]+(?:-[a-z]+)*", entry_id)
        assert any(re.search("[\u4e00-\u9fff]", alias) for alias in entry["aliases"])
        assert entry["applicability"] and len(entry["steps"]) >= 3
        assert all(part_exists(part) for part in entry["parts"])
        assert entry["sources"]
        for source in entry["sources"]:
            assert source["author"] == "Carlos Antelo"
            assert source["url"].startswith(NOVA_URL)
            assert source["license"] == "CC BY-SA 4.0"
            assert "reference" in source["adaptation"].lower()
            assert source["attribution_url"].startswith(NOVA_URL)
    assert "MIT" in CORPUS.joinpath("CONTENT-LICENSE.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("entry_id", sorted(REQUIRED_EXAMPLES))
def test_example_executes_with_its_declared_parts(entry_id):
    entry = json.loads(
        CORPUS.joinpath("entries", f"{entry_id}.json").read_text(encoding="utf-8")
    )
    assert entry["example"] == entry_id.replace("-", "_") + ".py"
    example = CORPUS.joinpath("examples", entry["example"])
    source = ast.parse(example.read_text(encoding="utf-8"))
    part_literals = {
        node.args[0].value
        for node in ast.walk(source)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"Element", "lookup_part"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    assert part_literals
    assert part_literals <= set(entry["parts"])
    codes = {
        node.value
        for node in ast.walk(source)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.fullmatch(
            r"(assembly|catalog|connector|motion|validation)\.[a-z_]+", node.value
        )
    }
    assert codes <= set(entry["diagnostics"])
    with as_file(example) as path:
        runpy.run_path(str(path), run_name="__main__")
