"""Exercise the reference topics installed for external builders."""

from bricklayer import Color, LegoBuild
from bricklayer.construction import registry
from bricklayer.docs import api_reference_pages
from bricklayer.docs.utils import document_function, document_members


def test_api_pages_link_to_the_topics_that_own_each_signature():
    pages = api_reference_pages()
    topics = {
        "parts.md": "lookup_part(",
        "placement.md": "stack(",
        "geometry.md": "Position(",
        "connections.md": "find_connection_pairs(",
        "motion.md": "check_motion(",
        "validation-export.md": "write_ldraw(",
    }
    for filename, symbol in topics.items():
        assert f"]({filename})" in pages["index.md"]
        assert symbol in pages[filename]
    for method, filename in {
        "add": "placement.md",
        "find_connection_candidates": "connections.md",
        "set_motion": "motion.md",
        "colliding_placements": "validation-export.md",
    }.items():
        docs = document_members(LegoBuild, members=(method,)).split("\n\n", 1)[1]
        assert docs in pages[filename]
        assert sum(docs in page for page in pages.values()) == 1


def test_api_pages_defer_palette_and_each_factory_table():
    pages = api_reference_pages()
    parts = pages["parts.md"]
    assert "](colors.md)" in parts
    assert repr(Color.OPAL_TRANS_LIGHT_BLUE) in pages["colors.md"]
    assert repr(Color.OPAL_TRANS_LIGHT_BLUE) not in parts
    for factory in registry.COMPONENT_FACTORIES:
        filename = f"factories/{factory.__name__}.md"
        assert document_function(factory, 3) in parts
        if registry.valid_part_kwarg_combos(factory):
            assert f"]({filename})" in parts
            assert "Valid argument combinations" in pages[filename]
        else:
            assert filename not in pages
    assert "Valid argument combinations" not in parts


def test_api_pages_render_live_source_documentation(monkeypatch):
    marker = "Unique candidate query documentation."
    monkeypatch.setattr(LegoBuild.find_connection_candidates, "__doc__", marker)
    assert marker in api_reference_pages()["connections.md"]
