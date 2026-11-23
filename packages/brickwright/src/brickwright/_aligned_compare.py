"""Verifier-compatible pixel metrics and two-dimensional differences."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Literal, TypedDict

import numpy as np
from PIL import Image

DifferenceKind = Literal["reference_only", "candidate_only", "color_mismatch"]
DIFFERENCE_COLORS: dict[DifferenceKind, tuple[int, int, int, int]] = {
    "reference_only": (255, 113, 64, 255),
    "candidate_only": (68, 167, 255, 255),
    "color_mismatch": (222, 76, 211, 255),
}
MAX_REGIONS = 12


class Region(TypedDict):
    kind: DifferenceKind
    bbox: list[int]
    area: int


class Metrics(TypedDict):
    silhouette_iou: float
    rgb_agreement_over_union: float
    regions: list[Region]


def read_rgba(path: Path | BytesIO) -> np.ndarray:
    with Image.open(path) as image:
        return np.array(image.convert("RGBA"))


def compare_images(
    reference: np.ndarray, candidate: np.ndarray
) -> tuple[Metrics, np.ndarray]:
    """Compare uint8 RGBA pixels and return metrics and a transparent difference image.

    Boxes use [left, top, right, bottom], with right and bottom excluded.
    Each region is a four-connected group of pixels in one difference category.
    """
    if (
        reference.shape != candidate.shape
        or reference.ndim != 3
        or reference.shape[2] != 4
    ):
        raise ValueError("comparison images must be RGBA and have the same dimensions")
    ref_foreground = reference[:, :, 3] >= 128
    candidate_foreground = candidate[:, :, 3] >= 128
    union = int(np.count_nonzero(ref_foreground | candidate_foreground))
    if not union:
        raise ValueError("empty foreground union")
    intersection = ref_foreground & candidate_foreground
    rgb_delta = np.abs(reference[:, :, :3].astype(np.int16) - candidate[:, :, :3])
    same = intersection & (np.max(rgb_delta, axis=2) <= 16)
    metrics: Metrics = {
        "silhouette_iou": float(np.count_nonzero(intersection) / union),
        "rgb_agreement_over_union": float(np.count_nonzero(same) / union),
        "regions": [],
    }
    masks: dict[DifferenceKind, np.ndarray] = {
        "reference_only": ref_foreground & ~candidate_foreground,
        "candidate_only": candidate_foreground & ~ref_foreground,
        "color_mismatch": intersection & ~same,
    }
    overlay = np.zeros(reference.shape, dtype=np.uint8)
    overlay[intersection, :3] = candidate[intersection, :3]
    overlay[intersection, 3] = 110
    regions = []
    for kind, mask in masks.items():
        overlay[mask] = DIFFERENCE_COLORS[kind]
        regions.extend(_regions(mask, kind))
    regions.sort(key=lambda region: (-region["area"], region["bbox"], region["kind"]))
    metrics["regions"] = regions[:MAX_REGIONS]
    return metrics, overlay


def _regions(mask: np.ndarray, kind: DifferenceKind) -> list[Region]:
    # Find horizontal runs with a padded edge, then union runs that overlap
    # the preceding row. Work scales with the number of runs, not filled area.
    changes = np.diff(mask, axis=1, prepend=False, append=False)
    rows, boundaries = np.nonzero(changes)
    parents: list[int] = []
    regions: list[Region] = []

    def root(index: int) -> int:
        while (parent := parents[index]) != index:
            parents[index] = parents[parent]
            index = parent
        return index

    def join(a: int, b: int) -> None:
        a, b = root(a), root(b)
        if a == b:
            return
        if regions[a]["area"] < regions[b]["area"]:
            a, b = b, a
        parents[b] = a
        first, second = regions[a]["bbox"], regions[b]["bbox"]
        regions[a]["bbox"] = [
            min(first[0], second[0]),
            min(first[1], second[1]),
            max(first[2], second[2]),
            max(first[3], second[3]),
        ]
        regions[a]["area"] += regions[b]["area"]

    previous: list[tuple[int, int, int]] = []
    current: list[tuple[int, int, int]] = []
    last_y, scan = -2, 0
    for y, left, right in zip(
        rows[::2].tolist(), boundaries[::2].tolist(), boundaries[1::2].tolist()
    ):
        if y != last_y:
            previous = current if y == last_y + 1 else []
            current, scan, last_y = [], 0, y
        index = len(parents)
        parents.append(index)
        regions.append(
            {"kind": kind, "bbox": [left, y, right, y + 1], "area": right - left}
        )
        current.append((left, right, index))
        while scan < len(previous) and previous[scan][1] <= left:
            scan += 1
        for prev in range(scan, len(previous)):
            prev_left, _, prev_index = previous[prev]
            if prev_left >= right:
                break
            join(index, prev_index)
    return [region for index, region in enumerate(regions) if parents[index] == index]
