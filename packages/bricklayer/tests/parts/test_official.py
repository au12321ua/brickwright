"""The official release index and the SDK catalog serve different callers."""

import os
from pathlib import Path

import pytest

import bricklayer as bl
from bricklayer.ldraw import partdata
from bricklayer.ldraw.conversion import LDrawVec


def test_official_coverage_and_buildability_totals():
    ids = bl.official_part_ids()
    assert len(ids) == 24_985
    assert {part.part_id for part in bl.search_official_parts(limit=None)} == ids
    constructible = bl.search_official_parts(constructible=True, limit=None)
    assert {part.part_id for part in constructible} == ids & partdata.part_ids()
    summary = bl.official_catalog_summary()
    for statuses in summary.values():
        assert sum(statuses.values()) == len(ids)


def test_official_id_set_matches_the_frozen_library():
    root = os.environ.get("BRICKLAYER_TEST_OFFICIAL_LDRAW")
    if not root:
        pytest.skip("set BRICKLAYER_TEST_OFFICIAL_LDRAW to the official snapshot")
    source_ids = {path.stem for path in (Path(root) / "parts").glob("*.dat")}
    assert bl.official_part_ids() == source_ids


def test_entries_distinguish_missing_capabilities_and_absent_ids():
    visual = bl.lookup_part("102525")
    assert visual.in_version
    assert visual.geometry.available
    assert visual.collision.reason_code == "missing_collision_mesh"
    assert visual.connectivity.status == "unverified"
    assert not visual.constructible.available
    assert not bl.part_exists(visual.part_id)
    with pytest.raises(bl.InvalidPartError, match=visual.constructible.reason) as exc:
        bl.PartSpec(visual.part_id)
    assert exc.value.code == "catalog.missing_data"
    assert exc.value.evidence["reason_code"] == visual.constructible.reason_code
    with pytest.warns(bl.LDrawImportWarning, match=visual.constructible.reason):
        model = bl.from_ldraw("1 4 0 0 0 1 0 0 0 1 0 0 0 1 102525.dat")
    assert model.brick_count == 0

    no_annotations = bl.lookup_part("10040")
    assert no_annotations.constructible.available
    assert no_annotations.collision.available
    assert no_annotations.connectivity.reason_code == "missing_connector_annotations"

    assert bl.lookup_part("003238a").connectivity.status == "not_applicable"
    unknown = bl.lookup_part("no-such-part")
    assert not unknown.in_version
    assert unknown.geometry.status == "outside_version"
    assert unknown.constructible.reason_code == "part_outside_version"
    with pytest.raises(ValueError, match="2026-01"):
        bl.lookup_part("3001", version="2026-01")


def test_identity_attribution_and_related_parts():
    alias = bl.lookup_part("15647")
    assert alias.kind == "alias"
    assert alias.canonical_id == "30390"
    assert alias.reference_transform_ldraw == (0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1)
    assert "15647" in bl.lookup_part("30390").aliases

    redirected = bl.lookup_part("100")
    assert redirected.kind == "redirect"
    assert redirected.canonical_id == "3665a"
    assert redirected.part_id == "100"

    pattern = bl.lookup_part("3001p01")
    assert pattern.kind == "pattern"
    assert pattern.variant_base_id == "3001"
    assert pattern.part_id in bl.lookup_part("3001").variants
    source = bl.lookup_part("3001").geometry.sources[0]
    assert source.file == "parts/3001.dat"
    assert source.author == "James Jessiman"
    assert "CC BY 4.0" in source.license
    assert len(source.sha256) == 64

    shortcut = bl.lookup_part("2651c01-f1")
    assert shortcut.kind == "assembly"
    assert set(shortcut.component_ids) == {"2650", "2651"}


def test_endpoint_geometry_is_the_same_as_the_sdk_connector():
    part = bl.lookup_part("11211")
    record = partdata.record(part.part_id)
    assert part.connectivity.status == "partial"
    assert tuple(endpoint.connector for endpoint in part.endpoints) == record.connectors
    for i, endpoint in enumerate(part.endpoints):
        assert endpoint.index == i
        assert endpoint.position_units == ("stud", "stud", "brick")
        assert endpoint.raw_position_units == "LDU"
        assert endpoint.ldraw_to_local == record.ldraw_to_element_pose
        assert endpoint.source.file == "part_connectivity.json"
        assert len(endpoint.raw.pos) == 3
        assert (
            endpoint.ldraw_to_local.apply(LDrawVec(*endpoint.raw.pos).to_position())
            == endpoint.connector.position
        )
    assert bl.lookup_part("58118").endpoints
    assert not bl.lookup_part("58118").constructible.available


def test_transformed_connector_donor_is_traceable():
    source = bl.lookup_part("10177").connectivity.sources[0]
    assert source.donor_id == "10177b"
    assert source.transform_ldraw == (0, 0, 1.25, 1, 0, 0, 0, 1, 0, 0, 0, 1)


def test_retired_sdk_id_and_pose_round_trip():
    source = "1 4 20 -24 10 0 0 -1 0 1 0 1 0 0 100.dat"
    model = bl.from_ldraw(source)
    assert model.brick_count == 1
    assert model.placements[0].content.ldraw_id == "100"
    assert source in bl.to_ldraw(model).splitlines()


def test_connector_index_and_official_search_avoid_constructing_candidates(monkeypatch):
    def fail(_):
        pytest.fail("discovery should not decode individual part records")

    monkeypatch.setattr(partdata, "record", fail)
    candidates = bl.parts_with_connectors("stud")
    assert "3001" in candidates
    assert "11211" in candidates
    assert "58118" not in candidates
    exact = bl.search_official_parts("3001.dat", limit=1)
    assert [part.part_id for part in exact] == ["3001"]
    named = bl.search_official_parts("hinge plate", constructible=True, limit=5)
    assert len(named) == 5
    assert all(
        part.constructible.available and "Hinge" in (part.name or "") for part in named
    )
    assert all(
        part.part_id in candidates
        for part in bl.search_official_parts("brick", connector_type="stud")
    )
    assert bl.search_official_parts("elongated nose", constructible=True) == ()


def test_supplemental_collision_and_connection_sources():
    hinge = bl.lookup_part("53923")
    assert hinge.constructible.available
    mesh = hinge.collision.sources[0]
    assert mesh.file == "meshes/Hinge.zip/53923.drc"
    assert mesh.donor_id == "53923"
    assert mesh.transform_ldraw == (0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1)
    assert any(source.file == "parts/53923.dat" for source in mesh.citations)
    assert {endpoint.raw.type for endpoint in hinge.endpoints} == {
        "axle_hole",
        "hinge_2_in",
    }
    assert all(
        endpoint.source.citations and endpoint.source.note
        for endpoint in hinge.endpoints
    )


def test_verified_mountings_expand_from_a_part_result():
    arm = bl.lookup_part("2651")
    assert arm.constructible.available
    assert arm.connectivity.available
    assert arm.verified_mountings == bl.verified_mountings("2651")
    assert {mounting.mounting for mounting in arm.verified_mountings} == {
        "retracted",
        "extended",
    }
    assert all(
        mounting.part_ids == ("2650", "2651") for mounting in arm.verified_mountings
    )
    assert [
        mounting.mounting
        for mounting in bl.lookup_part("2651c01-f2").verified_mountings
    ] == ["extended"]
    assert bl.lookup_part("no-such-part").verified_mountings == ()
