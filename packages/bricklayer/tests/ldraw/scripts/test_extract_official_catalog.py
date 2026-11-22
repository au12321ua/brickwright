"""Frozen release scans and offline updates report the underlying evidence."""

import json

from bricklayer.ldraw.scripts.extract_official_catalog import diff_catalogs, main
from bricklayer.ldraw.scripts.official_snapshot import scan_snapshot


def test_snapshot_membership_and_geometry_dependencies(tmp_path):
    def write(path, text):
        path = tmp_path / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    write("parts/01.dat", "0 Original\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 s\\a.dat\n")
    write("parts/02.dat", "0 ~Moved to 01\n1 16 10 0 0 1 0 0 0 1 0 0 0 1 01.dat\n")
    write("parts/03.dat", "0 Missing reference\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 b.dat\n")
    write("parts/s/a.dat", "0 A subpart\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 1-4cyli.dat\n")
    write("p/1-4cyli.dat", "0 Primitive\n3 16 0 0 0 1 0 0 1 1 0\n")
    write("unofficial/parts/b.dat", "0 Unofficial\n3 16 0 0 0 1 0 0 1 1 0\n")

    snapshot = scan_snapshot(tmp_path)
    assert set(snapshot.parts) == {"01", "02", "03"}
    assert snapshot.file_count == 5
    assert snapshot.parts["01"]["visual"] == "available"
    assert snapshot.parts["02"]["reference_ldraw"][:3] == [10, 0, 0]
    assert snapshot.parts["02"]["canonical_id"] == "01"
    assert snapshot.parts["03"]["visual"] == "missing"
    assert snapshot.parts["03"]["geometry_issues"] == ["b.dat"]
    assert scan_snapshot(tmp_path) == snapshot

    write("p/1-4cyli.dat", "0 Primitive changed\n3 16 0 0 0 1 0 0 1 1 0\n")
    changed = scan_snapshot(tmp_path)
    assert changed.sha256 != snapshot.sha256
    assert changed.ids_sha256 == snapshot.ids_sha256


def test_offline_diff_reports_membership_and_new_capabilities(tmp_path, capsys):
    metadata = {
        "version": "2026-10",
        "schema": 1,
        "inputs": {},
        "status_counts": {},
    }
    baseline = metadata | {
        "parts": {"new-collider": {"model": False}, "retired": {"model": True}},
        "mountings": {},
    }
    current = metadata | {
        "parts": {"new-collider": {"model": True}, "new-id": {"model": False}},
        "mountings": {"pair-1": "fingerprint"},
    }
    before, after = (tmp_path / name for name in ("before.json", "after.json"))
    before.write_text(json.dumps(baseline))
    after.write_text(json.dumps(current))
    diff = diff_catalogs(json.loads(before.read_text()), json.loads(after.read_text()))
    assert diff["added_ids"] == ["new-id"]
    assert diff["removed_ids"] == ["retired"]
    assert diff["newly_constructible"] == ["new-collider"]
    assert diff["changed_parts"] == {"new-collider": ["model"]}
    assert diff["newly_verified_mountings"] == ["pair-1"]
    assert main(["--compare", str(before), str(after)]) == 0
    assert json.loads(capsys.readouterr().out) == diff
