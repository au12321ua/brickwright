"""Isolated, controlled replay of an archived record under its original SDK."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from typing import TypedDict, cast

from bricklayer import LegoBuild, to_ldraw
from bricklayer.diagnostics import connector_evidence
from bricklayer.ldraw import partdata
from bricklayer.modeling.build import _is_grounded

from brickwright.flow.structure import inspect_build
from brickwright.tools.build_execution import load_build_module


class _Override(TypedDict):
    part_id: str
    instances: list[int]
    record_local: list[dict]
    original_sdk_default: list[dict] | None


class _ArchivedBuild:
    """Expose the contact queries using the baseline SDK's own graph caches."""

    def __init__(self, build: LegoBuild):
        self.build = build

    def __getattr__(self, name: str):
        return getattr(self.build, name)

    @property
    def mated_placement_pairs(self):
        leaves = self.build.flattened_placements
        return tuple((leaves[a], leaves[b]) for a, b in self.build._mating.pairs)

    @property
    def component_contact_pairs(self):
        leaves = self.build.flattened_placements
        return tuple((leaves[a], leaves[b]) for a, b in self.build._support_edges)

    @property
    def contact_only_placements(self):
        mated = {item for pair in self.mated_placement_pairs for item in pair}
        resting = {leaf for _, leaf in self.component_contact_pairs}
        return tuple(
            leaf
            for leaf in self.build.flattened_placements
            if leaf in resting
            and leaf not in mated
            and not _is_grounded(leaf, self.build._ground)
        )


def _ordered_connectors(connectors: Iterable) -> list[dict]:
    return sorted((connector_evidence(item) for item in connectors), key=repr)


def _catalog_endpoints(part_ids: list[str]) -> dict:
    result = {}
    for part_id in part_ids:
        try:
            result[part_id] = _ordered_connectors(
                partdata.record(part_id).connector_set
            )
        except (KeyError, ValueError):
            continue
    return result


def replay(record_path: Path, part_ids: list[str], input_dir: Path | None) -> dict:
    source_path = record_path / "build.py"
    original = source_path.read_bytes()
    source = original.decode("utf-8")
    replacement = str(input_dir) if input_dir is not None else None
    if replacement is not None:
        source = source.replace("/app/input", replacement)
    if (record_path / "merge-manifest.json").is_file():
        from brickwright.modules import load_merge, merge_context
        from brickwright.record import Record

        context = merge_context(load_merge(Record(record_path)))
    else:
        context = nullcontext()
    standard = _catalog_endpoints(part_ids)
    with context:
        model = load_build_module(source_path, source).create_build()
        mpd = to_ldraw(model).encode()
        wrapped = (
            model
            if hasattr(type(model), "component_contact_pairs")
            else _ArchivedBuild(model)
        )
        evidence = inspect_build(cast("LegoBuild", wrapped))
        overrides = []
        by_signature: dict[tuple[str, str], _Override] = {}
        for i, leaf in enumerate(model.flattened_placements):
            part_id = leaf.content.ldraw_id
            native_endpoints = _ordered_connectors(leaf.content.connectors)
            packaged = standard.get(part_id)
            if native_endpoints == packaged:
                continue
            signature = part_id, json.dumps(native_endpoints, sort_keys=True)
            row = by_signature.get(signature)
            if row is None:
                row = _Override(
                    part_id=part_id,
                    instances=[],
                    record_local=native_endpoints,
                    original_sdk_default=packaged,
                )
                by_signature[signature] = row
                overrides.append(row)
            row["instances"].append(i)
    return {
        "source_sha256": sha256(original).hexdigest(),
        "replay_source_sha256": sha256(source.encode()).hexdigest(),
        "remapped_input_dir": replacement,
        "mpd_sha256": sha256(mpd).hexdigest(),
        "column": evidence.model_dump(mode="json"),
        "endpoint_overrides": overrides,
    }


def main() -> None:
    request = json.loads(sys.argv[1])
    if library := request.get("ldraw_library"):
        os.environ["LDRAW_LIBRARY_PATH"] = library
    result = replay(
        Path(request["record_path"]),
        request["part_ids"],
        Path(request["input_dir"]) if request["input_dir"] else None,
    )
    Path(request["output"]).write_text(json.dumps(result, allow_nan=False))
