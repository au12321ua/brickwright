"""An official recipe participates in regular mesh extraction."""

import numpy as np
import pytest

from bricklayer.geometry.collision import SURFACE_INSET_LDU
from bricklayer.ldraw.meshes import encode_mesh
from bricklayer.ldraw.scripts.extract_part_meshes import _read_meshes, categorize
from bricklayer.ldraw.scripts.library import Library, catalog_sources
from bricklayer.ldraw.scripts.official_mesh import official_mesh

_CUBE = """0 Solid cube
0 BFC CERTIFY CCW
4 16 0 0 0 0 0 1 0 1 1 0 1 0
4 16 1 0 0 1 1 0 1 1 1 1 0 1
4 16 0 0 0 1 0 0 1 0 1 0 0 1
4 16 0 1 0 0 1 1 1 1 1 1 1 0
4 16 0 0 0 0 1 0 1 1 0 1 0 0
4 16 0 0 1 1 0 1 1 1 1 0 1 1
"""


def test_supplement_selects_and_generates_an_inset_official_mesh(
    ldraw_root, bricknet_data, tmp_path
):
    root = ldraw_root(
        {
            "parts/53923.dat": (
                "0 Mirrored cuboid\n"
                "0 BFC CERTIFY CCW\n"
                "1 16 9 12 -2 -4 0 0 0 3 0 0 0 2 synthetic-cube.dat\n"
            ),
            "p/synthetic-cube.dat": _CUBE,
        }
    )
    mesh_dir = tmp_path / "empty-inset"
    mesh_dir.mkdir()
    lib = Library(root)
    selected = catalog_sources(lib, mesh_dir=mesh_dir, bricknet_data=bricknet_data)
    assert selected.part_ids == ["53923"]
    categories, missing, uncategorized = categorize(
        lib, selected.part_ids, selected.meshes
    )
    assert not missing and not uncategorized
    category, mesh = _read_meshes(mesh_dir, categories, selected.meshes, lib=lib)[
        "53923"
    ]
    assert category == "Mirrored"
    low, high = mesh.bounds
    assert np.all(low >= np.array([5, 12, -2]) + SURFACE_INSET_LDU)
    assert np.all(high <= np.array([9, 15, 0]) - SURFACE_INSET_LDU)
    assert np.all(high - low > np.array([3, 2, 1]))

    regenerated, inputs = official_mesh(lib, "53923")
    assert encode_mesh(regenerated) == encode_mesh(mesh)
    assert inputs == (root / "p/synthetic-cube.dat", root / "parts/53923.dat")


def test_official_recipe_rejects_missing_dependencies(ldraw_root):
    lib = Library(
        ldraw_root(
            {"parts/53923.dat": "0 Hinge\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 missing.dat\n"}
        )
    )
    with pytest.raises(ValueError, match="Missing official geometry: missing.dat"):
        official_mesh(lib, "53923")
