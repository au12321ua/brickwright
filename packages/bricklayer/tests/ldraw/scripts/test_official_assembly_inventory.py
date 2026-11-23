from pathlib import Path

from bricklayer.ldraw.scripts.official_assembly_inventory import (
    CoveredInterface,
    assembly_ids,
    read_assembly,
    scan_inventory,
)
from bricklayer.ldraw.scripts.official_snapshot import scan_snapshot


def write_part(root: Path, part_id: str, body: str) -> None:
    target = root / "parts" / f"{part_id}.dat"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)


def test_assembly_inventory_keeps_shortcut_aliases_and_mixed_interfaces(tmp_path):
    write_part(
        tmp_path,
        "root",
        """0 Cabinet with two doors
0 Author: Builder
0 !LDRAW_ORG Shortcut UPDATE 2026-10
0 !LICENSE CC BY 4.0
1 4 10 20 30 0 0 1 0 1 0 -1 0 0 sub.dat
1 2 10 0 0 1 0 0 0 1 0 0 0 1 leaf.dat
1 2 20 0 0 1 0 0 0 1 0 0 0 1 leaf.dat
1 2 0 0 0 1 0 0 0 1 0 0 0 1 lost.dat
1 16 0 0 0 2 0 0 0 1 0 0 0 1 leaf.dat
""",
    )
    write_part(
        tmp_path,
        "sub",
        """0 Subassembly
0 !LDRAW_ORG Shortcut
1 16 1 2 3 1 0 0 0 1 0 0 0 1 leaf.dat
""",
    )
    write_part(
        tmp_path,
        "old",
        """0 ~Moved to root
0 !LDRAW_ORG Shortcut
1 16 0 0 0 1 0 0 0 1 0 0 0 1 root.dat
""",
    )
    write_part(tmp_path, "leaf", "0 Door Leaf\n0 !LDRAW_ORG Part\n")
    snapshot = scan_snapshot(tmp_path)

    assert assembly_ids(snapshot) == ("old", "root", "sub")
    observed = read_assembly(tmp_path, "root", snapshot)
    assert len(observed.root_lines) == 5
    assert [item.part_id for item in observed.direct] == ["sub", "leaf", "leaf", "leaf"]
    first, second, third, _ = observed.instances
    assert first.transform[:3] == (13, 22, 29)
    assert first.color == 4
    assert [step.line_number for step in first.path] == [5, 3]
    assert first.path[0].author == "Builder"
    assert first.path[0].license == "CC BY 4.0"
    assert first.path[0].sha256 == snapshot.parts["root"]["sha256"]
    assert second.path != third.path
    assert {issue.code for issue in observed.issues} == {
        "non_rigid_transform",
        "unresolved_reference",
    }

    rows = scan_inventory(
        tmp_path,
        snapshot,
        {"root": [CoveredInterface("root:left", "hinge", first, second)]},
    )
    by_id = {row["assembly_id"]: row for row in rows}
    assert tuple(by_id) == ("old", "root", "sub")
    assert by_id["old"]["canonical_id"] == "root"
    assert by_id["old"]["coverage"] == "unverified"
    assert by_id["root"]["coverage"] == "partial"
    assert "root:left" in str(by_id["root"]["interfaces"])
    assert "unresolved_reference" in str(by_id["root"]["issues"])


def test_nonphysical_type1_and_flex_are_distinguished(tmp_path):
    write_part(
        tmp_path,
        "wrap",
        """0 Flexible hose with decal
0 !LDRAW_ORG Shortcut
0 !: 1 16 0 0 0 1 0 0 0 1 0 0 0 1 hose.dat
1 4 0 0 0 1 0 0 0 1 0 0 0 1 sticker.dat
1 16 0 0 0 1 0 0 0 1 0 0 0 1 s\\surface.dat
""",
    )
    write_part(tmp_path, "hose", "0 Hose Segment\n0 !LDRAW_ORG Part Flexible_Section\n")
    write_part(tmp_path, "sticker", "0 Sticker A\n0 !LDRAW_ORG Part\n")
    write_part(tmp_path, "s/surface", "0 geometric subpart\n")
    snapshot = scan_snapshot(tmp_path)
    (row,) = scan_inventory(tmp_path, snapshot, {})
    assert row["coverage"] == "unverified"
    for reason in ("flexible_shape_required", "decorative_only", "geometry_only"):
        assert reason in str(row)
