"""Locations for validation findings in a nested BrickLayer model."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import fields
from typing import TYPE_CHECKING

from bricklayer.diagnostics import connector_evidence
from bricklayer.parts.element import Element

if TYPE_CHECKING:
    from bricklayer import Joint, JointCoordinates, LegoBuild, Placement
    from bricklayer.modeling.articulation import JointEndpoint
    from pydantic import JsonValue


class PlacementEvidence:
    """Map flattened leaves and joint endpoints back to their occurrences."""

    def __init__(self, build: LegoBuild) -> None:
        self._paths: dict[Placement[Element], tuple[str | int, ...] | None] = {}
        self._leaves = {}
        for leaf, (path, labels) in zip(
            build.flattened_placements, _leaf_occurrences(build)
        ):
            self._paths[leaf] = None if leaf in self._paths else labels
            self._leaves[path] = leaf

    def placement(self, leaf: Placement[Element]) -> dict[str, JsonValue]:
        facts: dict[str, JsonValue] = {
            "part_id": leaf.content.ldraw_id,
            "position": [float(value) for value in leaf.pose.translation],
        }
        labels = self._paths.get(leaf)
        if labels is not None:
            facts["path"] = list(labels)
        return facts

    def joint(self, joint: Joint) -> dict[str, JsonValue]:
        return {
            "parent": self._endpoint(joint.parent),
            "child": self._endpoint(joint.child),
        }

    def positions(
        self, positions: tuple[tuple[Joint, JointCoordinates], ...]
    ) -> list[JsonValue]:
        return [
            {
                **self.joint(joint),
                "kind": coordinates.kind.name.lower(),
                "coordinates": {
                    field.name: float(getattr(coordinates, field.name))
                    for field in fields(coordinates)
                },
            }
            for joint, coordinates in positions
        ]

    def _endpoint(self, endpoint: JointEndpoint) -> dict[str, JsonValue]:
        leaf = self._leaves[endpoint.path]
        return self.placement(leaf) | {
            "connector": connector_evidence(endpoint.connector)
        }


def _leaf_occurrences(
    build: LegoBuild,
    path: tuple[Placement, ...] = (),
    labels: tuple[str | int, ...] = (),
) -> Iterator[tuple[tuple[Placement, ...], tuple[str | int, ...]]]:
    for index, placement in enumerate(build.placements):
        child_path = (*path, placement)
        name = build.name_of(placement)
        child_labels = (*labels, name if name is not None else index)
        if isinstance(placement.content, Element):
            yield child_path, child_labels
        else:
            yield from _leaf_occurrences(placement.content, child_path, child_labels)
