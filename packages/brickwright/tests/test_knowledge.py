import json

import pytest
from typer.testing import CliRunner

from brickwright import knowledge
from brickwright.cli import app
from brickwright.prompts import system_prompt
from brickwright.tools import execute_tool


@pytest.fixture
def knowledge_resources(monkeypatch, tmp_path):
    entries = tmp_path / "entries"
    entries.mkdir()
    examples = tmp_path / "examples"
    examples.mkdir()
    case = {
        "id": "swinging-flap",
        "title": "Mount a swinging flap",
        "aliases": ["hatch clears bodywork", "舱盖打开间隙"],
        "topics": ["aircraft"],
        "actions": ["sample hinge travel"],
        "applicability": "Use when a flap rotates around a rigid hinge.",
        "parts": ["3937", "3938a"],
        "diagnostics": ["motion.collision"],
        "steps": ["Check the hinge axis before sampling.", "Move the hinge inward."],
        "example": "swinging_flap.py",
        "expected": "The hinge mates and the flap swings clear.",
        "sources": [
            {
                "author": "A. Builder",
                "title": "Hinge reference",
                "url": "https://example.test/reference",
                "license": "CC BY-SA 4.0",
                "adaptation": "Topic reference only. Original example.",
                "attribution_url": "https://example.test/attribution",
            },
        ],
    }
    (entries / "swinging-flap.json").write_text(json.dumps(case), encoding="utf-8")
    case.update(
        id="clamping-flap",
        title="Clamp a static flap",
        aliases=[],
        topics=[],
        actions=[],
        parts=["3937"],
        diagnostics=[],
        example=None,
    )
    (entries / "clamping-flap.json").write_text(json.dumps(case), encoding="utf-8")
    example = examples / "swinging_flap.py"
    example.write_text("def check():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(knowledge, "_RESOURCES", tmp_path)
    knowledge._entries.cache_clear()
    yield example
    knowledge._entries.cache_clear()


@pytest.mark.usefixtures("knowledge_resources")
@pytest.mark.parametrize(
    "query,match_field,term",
    [
        ("bodywork for hatches", "aliases", "bodywork"),
        ("3938A.DAT", "parts", "3938a"),
        ("Observed MOTION.COLLISION", "diagnostics", "motion.collision"),
        ("舱盖合得上却打不开", "aliases", "舱盖"),
        ("hinge travel", "actions", "hinge"),
        ("aircraft", "topics", "aircraft"),
        ("swinging flap", "title", "flap"),
    ],
)
def test_search_explains_the_match(query, match_field, term):
    result = knowledge.search_knowledge(query, limit=1)
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.id == "swinging-flap"
    assert term in match.matched_on[match_field]
    assert match.applicability.startswith("Use when")
    assert result.message is None


@pytest.mark.usefixtures("knowledge_resources")
@pytest.mark.parametrize("query", ["", "!?", "organic persimmon", "193938a"])
def test_search_reports_no_matches(query):
    result = knowledge.search_knowledge(query)
    assert result.matches == ()
    assert result.message == "No matching knowledge entries."


def test_only_read_loads_the_example(knowledge_resources):
    content = knowledge_resources.read_text(encoding="utf-8")
    article = knowledge.read_knowledge("swinging-flap")
    assert article.example is not None
    assert article.example.source == content
    assert article.example.filename == "swinging_flap.py"
    assert article.sources[0].license == "CC BY-SA 4.0"
    assert article.sources[0].adaptation
    assert article.sources[0].attribution_url
    assert article.steps[1] == "Move the hinge inward."
    assert article.expected
    knowledge_resources.unlink()
    assert knowledge.search_knowledge("hatch").matches
    assert knowledge.read_knowledge("clamping-flap").example is None


@pytest.mark.usefixtures("knowledge_resources")
def test_read_requires_an_entry_id_and_has_no_path_lookup():
    with pytest.raises(KeyError):
        knowledge.read_knowledge("../examples/swinging_flap.py")


@pytest.mark.usefixtures("knowledge_resources")
def test_cli_and_tools_use_the_same_search_and_read_results(config, record):
    runner = CliRunner()
    query = "aircraft hatch"
    for command, name, argument, value, expected in [
        (
            "search",
            "search_building_knowledge",
            "query",
            query,
            knowledge.search_knowledge(query),
        ),
        (
            "show",
            "read_building_knowledge",
            "entry_id",
            "swinging-flap",
            knowledge.read_knowledge("swinging-flap"),
        ),
    ]:
        cli = runner.invoke(app, ["knowledge", command, value])
        tool = execute_tool(record, config, name, {argument: value})
        assert cli.exit_code == 0, cli.output
        assert tool.ok
        assert (
            json.loads(cli.stdout)
            == json.loads(tool.content)
            == expected.model_dump(mode="json")
        )


@pytest.mark.usefixtures("knowledge_resources")
def test_unknown_entry_is_a_read_error(config, record):
    cli = CliRunner().invoke(app, ["knowledge", "show", "unknown-entry"])
    tool = execute_tool(
        record, config, "read_building_knowledge", {"entry_id": "unknown-entry"}
    )
    assert cli.exit_code == 1
    assert not tool.ok
    assert "unknown-entry" in cli.output and "unknown-entry" in tool.content


def test_agent_instructions_offer_their_knowledge_entry_points():
    external = system_prompt(external=True)
    internal = system_prompt()
    assert "brickwright knowledge search" in external
    assert "brickwright knowledge show" in external
    assert "search_building_knowledge" in internal
    assert "read_building_knowledge" in internal
