"""Conservative collision geometry from the triangle surfaces of official parts."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from ...geometry.collision import SURFACE_INSET_LDU
from ..meshes import Mesh

if TYPE_CHECKING:
    from .library import Library

# Individual parts added by this recipe. The regular catalog still uses BrickNet
# meshes and rigid donors for the rest of the library.
GENERATED_MESH_IDS = frozenset({"53923"})
CELL_LDU = 0.15


def official_mesh(lib: Library, part_id: str) -> tuple[Mesh, tuple[Path, ...]]:
    """Generate an inset mesh and list every official file read to generate it.

    The mesh is the boundary of solid voxels. A voxel is occupied if its center
    is inside the BFC-oriented shell along at least two of the three ray axes.
    Each occupied cell is at least SURFACE_INSET_LDU from the nearest
    triangulated LDraw surface.
    """
    inputs: set[Path] = set()
    triangles = np.array(
        list(
            _triangles(
                lib,
                f"{part_id}.dat",
                np.eye(3),
                np.zeros(3),
                inverted=False,
                stack=(),
                inputs=inputs,
            )
        )
    )
    if not len(triangles):
        raise ValueError(f"{part_id}: no triangles")
    grid = _grid(triangles)
    votes = np.zeros(tuple(len(axis) for axis in grid), dtype=np.uint8)
    for primary in range(3):
        order = (primary, (primary + 1) % 3, (primary + 2) % 3)
        counts = _winding_counts(triangles[:, :, order], tuple(grid[i] for i in order))
        votes += (counts < 0).transpose(np.argsort(order))
    occupied = votes >= 2
    _clear_surfaces(occupied, triangles, grid)
    if not occupied.any():
        raise ValueError(f"{part_id}: no enclosed solid geometry")
    return _voxel_surface(occupied, grid), tuple(sorted(inputs))


def _triangles(
    lib: Library,
    name: str,
    matrix: np.ndarray,
    translation: np.ndarray,
    *,
    inverted: bool,
    stack: tuple[Path, ...],
    inputs: set[Path],
) -> Iterator[np.ndarray]:
    path = lib.resolve(name)
    if path is None or path.relative_to(lib.root).parts[0] not in {"p", "parts"}:
        raise ValueError(f"Missing official geometry: {name}")
    if path in stack:
        raise ValueError(f"Cyclic LDraw reference: {name}")
    inputs.add(path)
    reverse = inverted
    invert_next = False
    mirrored = np.linalg.det(matrix) < 0
    for line in lib.lines(name) or ():
        fields = line.split()
        match fields:
            case ["0", "BFC", *words] if "CW" in words or "CCW" in words:
                reverse = inverted ^ ("CW" in words)
            case ["0", "BFC", "INVERTNEXT"]:
                invert_next = True
            case ["1", _, *reference] if len(reference) >= 13:
                values = np.array(reference[:12], dtype=float)
                yield from _triangles(
                    lib,
                    " ".join(reference[12:]),
                    matrix @ values[3:].reshape(3, 3),
                    matrix @ values[:3] + translation,
                    inverted=inverted ^ invert_next,
                    stack=(*stack, path),
                    inputs=inputs,
                )
                invert_next = False
            case ["3" | "4", _, *coordinates]:
                vertices = np.array(coordinates, dtype=float).reshape(-1, 3)
                vertices = vertices @ matrix.T + translation
                if reverse ^ mirrored:
                    vertices = vertices[::-1]
                yield vertices[:3]
                if len(vertices) == 4:
                    yield vertices[[0, 2, 3]]


def _grid(triangles: np.ndarray) -> tuple[np.ndarray, ...]:
    points = triangles.reshape(-1, 3)
    low = np.floor(points.min(axis=0) / CELL_LDU) - 1
    high = np.ceil(points.max(axis=0) / CELL_LDU) + 1
    # Move samples off shared face and edge coordinates. The offsets are fixed,
    # which makes the output independent of filesystem ordering or a random seed.
    return tuple(
        (np.arange(a, b) + shift) * CELL_LDU
        for a, b, shift in zip(low, high, (0.147, 0.321, 0.485))
    )


def _subgrid(
    grid: tuple[np.ndarray, ...], low: np.ndarray, high: np.ndarray
) -> tuple[slice, ...]:
    return tuple(
        slice(
            int(np.searchsorted(axis, start)),
            int(np.searchsorted(axis, end, side="right")),
        )
        for axis, start, end in zip(grid, low, high)
    )


def _winding_counts(triangles: np.ndarray, grid: tuple[np.ndarray, ...]) -> np.ndarray:
    crossings = np.zeros(tuple(len(axis) for axis in grid), dtype=np.int16)
    for a, b, c in triangles:
        ab, ac = b - a, c - a
        projected_area = ab[1] * ac[2] - ab[2] * ac[1]
        if abs(projected_area) < 1e-9:
            continue
        corners = np.stack((a, b, c))
        column, row = _subgrid(
            grid[1:], corners[:, 1:].min(axis=0), corners[:, 1:].max(axis=0)
        )
        y = grid[1][column][:, None] - a[1]
        z = grid[2][row][None, :] - a[2]
        u = (y * ac[2] - z * ac[1]) / projected_area
        v = (ab[1] * z - ab[2] * y) / projected_area
        j, k = np.where((u >= 0) & (v >= 0) & (u + v <= 1))
        hits = a[0] + u[j, k] * ab[0] + v[j, k] * ac[0]
        x = np.searchsorted(grid[0], hits)
        crossings[x, j + column.start, k + row.start] += 1 if projected_area > 0 else -1
    return np.cumsum(crossings, axis=0, dtype=np.int16)


def _clear_surfaces(
    occupied: np.ndarray, triangles: np.ndarray, grid: tuple[np.ndarray, ...]
) -> None:
    clearance = SURFACE_INSET_LDU + np.sqrt(3) * CELL_LDU / 2
    for triangle in triangles:
        subgrid = _subgrid(
            grid, triangle.min(axis=0) - clearance, triangle.max(axis=0) + clearance
        )
        solid = occupied[subgrid]
        if not solid.any():
            continue
        indices = np.where(solid)
        points = np.column_stack(
            [axis[span][index] for axis, span, index in zip(grid, subgrid, indices)]
        )
        distance = _point_distances_squared(points, triangle)
        solid[tuple(axis[distance < clearance**2] for axis in indices)] = False


def _point_distances_squared(points: np.ndarray, triangle: np.ndarray) -> np.ndarray:
    a, b, c = triangle
    ab, ac = b - a, c - a
    normal = np.cross(ab, ac)
    aa, bb, abac = ab @ ab, ac @ ac, ab @ ac
    area_squared = normal @ normal
    distances = np.full(len(points), np.inf)
    if area_squared > 1e-12:
        pa, pb = (points - a) @ ab, (points - a) @ ac
        u = (bb * pa - abac * pb) / area_squared
        v = (aa * pb - abac * pa) / area_squared
        on_face = (u >= 0) & (v >= 0) & (u + v <= 1)
        distances[on_face] = ((points[on_face] - a) @ normal) ** 2 / area_squared
    for start, end in ((a, b), (b, c), (c, a)):
        edge = end - start
        squared_length = edge @ edge
        delta = points - start
        if squared_length > 1e-12:
            delta -= np.clip(delta @ edge / squared_length, 0, 1)[:, None] * edge
        np.minimum(distances, np.einsum("ij,ij->i", delta, delta), out=distances)
    return distances


def _voxel_surface(occupied: np.ndarray, grid: tuple[np.ndarray, ...]) -> Mesh:
    padded = np.pad(occupied, 1)
    corners = []
    offsets = np.eye(3, dtype=int)
    for axis in range(3):
        j, k = (axis + 1) % 3, (axis + 2) % 3
        quad = np.stack(
            [np.zeros(3, dtype=int), offsets[j], offsets[j] + offsets[k], offsets[k]]
        )
        for side in (0, 1):
            adjacent = [slice(1, -1)] * 3
            adjacent[axis] = slice(2, None) if side else slice(None, -2)
            exposed = np.argwhere(occupied & ~padded[tuple(adjacent)])
            if side:
                face = quad + offsets[axis]
            else:
                face = quad[::-1]
            corners.append(exposed[:, None, :] + face)
    polygons = np.concatenate(corners)
    vertices, indices = np.unique(polygons.reshape(-1, 3), axis=0, return_inverse=True)
    quads = indices.reshape(-1, 4)
    faces = np.concatenate((quads[:, [0, 1, 2]], quads[:, [0, 2, 3]])).astype(np.int32)
    origin = np.array([axis[0] for axis in grid]) - CELL_LDU / 2
    return Mesh(origin + CELL_LDU * vertices, faces)
