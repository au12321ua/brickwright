"""Audit both OMR36 arms' exact exported MPDs using one frozen standard SDK."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import warnings
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from typing import Any
from zipfile import ZipFile

import bricklayer
from bricklayer import LegoBuild, from_ldraw
from bricklayer.io.reader import LDrawImportWarning

from brickwright.evaluation.omr36_blind import TASKS
from brickwright.flow.artifacts import atomic_write
from brickwright.flow.state import PublishedStructure
from brickwright.flow.structure import (
    Column,
    StructureReport,
    catalog_sha256,
    inspect_build,
    sdk_version,
)

ARTIFACTS = Path("artifacts/logs/artifacts")
BUS = "R-10258"
MAX_BLIND_SAMPLES = 8
_TASK = re.compile(r"R-[0-9]+\Z")
_ATTEMPT = re.compile(r"attempt-([1-9][0-9]*)\Z")

_WORKER = """
import json
import sys
paths, evaluator = json.loads(sys.argv[1])
sys.argv.pop(1)
sys.path[:0] = paths
import brickwright
brickwright.__path__.append(evaluator)
import brickwright.evaluation
brickwright.evaluation.__path__.append(evaluator + "/evaluation")
from brickwright.evaluation.omr36_native import main
main()
"""


def _digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def _sha(path: Path) -> str:
    return _digest(path.read_bytes())


def _read(path: Path) -> dict:
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise TypeError(f"Expected a JSON object in {path.name}")
    return data


def _write(path: Path, report: dict) -> None:
    data = json.dumps(report, indent=2, allow_nan=False, ensure_ascii=False) + "\n"
    atomic_write(path, data.encode())


def sdk_provenance(frozen_wheel: Path | None = None) -> dict:
    """Bind every archived trial to the same measured standard catalog and SDK files."""
    assert bricklayer.__file__ is not None
    package = Path(bricklayer.__file__).resolve().parent
    files = {
        path.relative_to(package).as_posix(): _sha(path)
        for path in package.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if frozen_wheel is not None:
        with ZipFile(frozen_wheel) as wheel:
            frozen = {
                member.filename.removeprefix("bricklayer/"): _digest(wheel.read(member))
                for member in wheel.infolist()
                if member.filename.startswith("bricklayer/") and not member.is_dir()
            }
        difference = sorted(
            name
            for name in files.keys() | frozen.keys()
            if files.get(name) != frozen.get(name)
        )
        if difference:
            raise ValueError(
                f"The standard SDK differs from the frozen wheel: {difference[:8]}"
            )
    package_hash = _digest(json.dumps(files, sort_keys=True).encode())
    return {
        "version": sdk_version(),
        "package_sha256": package_hash,
        "catalog_sha256": catalog_sha256(),
        "frozen_wheel_sha256": _sha(frozen_wheel) if frozen_wheel is not None else None,
    }


@dataclass(frozen=True)
class Replay:
    packages: tuple[Path, ...] = ()
    dataset: Path | None = None
    ldraw_library: Path | None = None
    revision: str | None = None
    timeout: float = 120

    def run(self, record: Path, task: str, standard: Column | None) -> dict:
        """Copy record evidence before executing any archived source."""
        with TemporaryDirectory(prefix="omr36-structure-") as temporary:
            scratch = Path(temporary)
            copied = scratch / record.name
            shutil.copytree(record, copied)
            inputs = None
            if self.dataset is not None:
                source = self.dataset / task / "environment" / "input"
                if source.is_dir():
                    inputs = scratch / "input"
                    shutil.copytree(source, inputs)
            evaluator = Path(__file__).resolve().parents[1]
            output = scratch / "result.json"
            request = {
                "record_path": str(copied),
                "output": str(output),
                "part_ids": sorted({loc.part_id for loc in standard.instances})
                if standard
                else [],
                "input_dir": str(inputs) if inputs else None,
                "ldraw_library": str(self.ldraw_library.resolve())
                if self.ldraw_library
                else None,
            }
            packages = [str(path.resolve()) for path in self.packages]
            command = [
                sys.executable,
                "-I",
                "-c",
                _WORKER,
                json.dumps([packages, str(evaluator)]),
                json.dumps(request),
            ]
            env = {
                key: value
                for key in ("PATH", "LANG", "LC_ALL")
                if (value := os.environ.get(key))
            }
            try:
                proc = subprocess.run(
                    command,
                    check=False,
                    cwd=scratch,
                    env=env,
                    capture_output=True,
                    timeout=self.timeout,
                )
                if proc.returncode == 0:
                    return _read(output) | {
                        "status": "observed",
                        "revision": self.revision,
                    }
                error = proc.stderr.decode("utf-8", "replace")[-1800:]
                return {"status": "error", "revision": self.revision, "error": error}
            except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
                return {"status": "error", "revision": self.revision, "error": str(exc)}


def _neutral_location(column: Column, index: int) -> dict:
    instance = column.instances[index]
    return {
        "part_id": instance.part_id,
        "position": [round(value, 6) for value in instance.position],
        "rotation": [[round(value, 6) for value in row] for row in instance.rotation],
    }


def _bus_support(build: LegoBuild, column: Column) -> dict | None:
    """Quantify direct bonds crossing the grounded tire-top plane when identifiable."""
    leaves = build.flattened_placements
    if not leaves:
        return None
    boxes = [leaf.bounding_box for leaf in leaves]
    low = min(float(box[0].z) for box in boxes)
    grounded_tires = [
        i
        for i, (leaf, box) in enumerate(zip(leaves, boxes, strict=True))
        if leaf.content.spec.name.casefold().startswith(("tyre ", "tire "))
        and abs(float(box[0].z) - low) < 0.02
    ]
    if len(grounded_tires) < 2:
        return None
    plane = median(float(boxes[i][1].z) for i in grounded_tires)
    lower = {
        i
        for i, box in enumerate(boxes)
        if (float(box[0].z) + float(box[1].z)) / 2 < plane
    }
    if not lower or len(lower) == len(leaves):
        return None
    ids = {leaf: i for i, leaf in enumerate(leaves)}

    def crossing(pairs) -> list[tuple[int, int]]:
        return [
            (a, b) if a in lower else (b, a)
            for first, second in pairs
            if ((a := ids[first]) in lower) != ((b := ids[second]) in lower)
        ]

    bonds = crossing(build.mated_placement_pairs)
    contacts = crossing(build.component_contact_pairs)
    largest = max(column.components, key=lambda component: component.part_count)
    return {
        "basis": "grounded tire-top plane",
        "height_bricks": round(plane, 6),
        "grounded_tire_count": len(grounded_tires),
        "grounded_tires_in_largest_component": len(
            set(largest.members).intersection(grounded_tires)
        ),
        "mated_pairs_crossing": len(bonds),
        "contact_pairs_crossing": len(contacts),
        "mated_pair_samples": [
            {
                "lower": _neutral_location(column, a),
                "upper": _neutral_location(column, b),
            }
            for a, b in bonds[:4]
        ],
    }


def _standard_read(raw: bytes, task: str) -> tuple[Column, dict | None, list[str]]:
    """The evaluation process never imports record code or a user LDraw library."""
    text = raw.decode("utf-8")
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", LDrawImportWarning)
        build = from_ldraw(text, ldraw_library="")
    column = inspect_build(build)
    column.warnings = [
        str(warning.message)
        for warning in captured
        if issubclass(warning.category, LDrawImportWarning)
    ]
    issues = [
        message
        for message in column.warnings
        if not message.startswith("used neutral appearance for")
    ]
    if any(
        line.split(maxsplit=1)[0] in {"2", "3", "4", "5"}
        for line in text.splitlines()
        if line.strip()
    ):
        issues.append("MPD contains raw primitives outside the element geometry")
    if not column.part_count:
        issues.append("standard catalog imported no parts")
    return column, _bus_support(build, column) if task == BUS else None, issues


def _original_native(
    product: Path, details: dict, digest: str
) -> tuple[Path | None, dict | None, list[str]]:
    identifier = details.get("record_id")
    if (
        not isinstance(identifier, str)
        or not identifier
        or Path(identifier).name != identifier
    ):
        return None, None, ["verifier did not identify a finalized record"]
    root = product / "records"
    record = root / identifier
    if not record.resolve().is_relative_to(root.resolve()):
        return None, None, ["record locator escapes artifact directory"]
    try:
        source = _sha(record / "build.py")
        meta = _read(record / "meta.json")
        if (
            meta.get("finalized") is not True
            or meta.get("exported_source_sha256") != source
        ):
            raise ValueError("record was not finalized for its archived source")
        if _sha(record / "out/build.ldr") != digest:
            raise ValueError("record export bytes differ from the submission")
    except (OSError, ValueError, TypeError) as exc:
        return None, None, [str(exc)]

    archive: dict[str, Any] = {"source_sha256": source}
    issues = []
    report = record / "out/report.json"
    try:
        validation = _read(report)
        if validation.get("source_sha256") != source:
            raise ValueError("native report is for a different source")
        archive["validation"] = {
            key: validation.get(key)
            for key in ("ok", "brick_count", "checks", "diagnostics")
        } | {"sha256": _sha(report)}
    except (OSError, ValueError, TypeError) as exc:
        issues.append(str(exc))

    pointer = record / "flow/current.json"
    if pointer.exists() or meta.get("flow"):
        try:
            current = PublishedStructure.model_validate_json(pointer.read_bytes())
            if not re.fullmatch(r"[0-9a-f]{64}", current.snapshot_id):
                raise ValueError("invalid native structure snapshot ID")
            directory = record / "flow/snapshots" / current.snapshot_id
            path = directory / "structure.json"
            if (
                current.inputs.source_sha256 != source
                or current.mpd_sha256 != digest
                or _sha(directory / "build.ldr") != digest
                or _sha(path) != current.structure_sha256
            ):
                raise ValueError("native structure snapshot does not bind the delivery")
            report = StructureReport.model_validate_json(path.read_bytes())
            if report.mpd_sha256 != digest or report.native is None:
                raise ValueError("native structure report is incomplete")
            archive["structure"] = report.model_dump(mode="json") | {
                "sha256": _sha(path)
            }
        except (OSError, ValueError, TypeError) as exc:
            issues.append(str(exc))
    else:
        path = record / "out/connectivity/components.json"
        try:
            metadata = _read(path)
            groups = [group["part_indices"] for group in metadata["components"]]
            count = details.get("pieces")
            members = sorted(index for group in groups for index in group)
            if (
                type(count) is not int
                or members != list(range(count))
                or metadata["component_count"] != len(groups)
                or any(not group for group in groups)
            ):
                raise ValueError(
                    "native connectivity evidence does not partition the delivered part count"
                )
            archive["connectivity"] = {
                "component_sizes": sorted(map(len, groups), reverse=True),
                "components": groups,
                "sha256": _sha(path),
            }
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append(str(exc))
    return record, archive, issues


def _sizes(column: Column) -> list[int]:
    return sorted(
        (component.part_count for component in column.components), reverse=True
    )


def _replay_agreement(
    replay: dict, archive: dict, standard: Column | None, delivered_sha: str
) -> dict:
    if replay["status"] != "observed":
        return replay
    column = Column.model_validate(replay["column"])
    archived_groups = (archive.get("connectivity") or {}).get("components")
    archived_validation = archive.get("validation") or {}
    replay_groups = {frozenset(group.members) for group in column.components}
    return replay | {
        "exact_delivery_bytes": replay["mpd_sha256"] == delivered_sha,
        "matches_archived_native_part_count": (
            column.part_count == archived_validation["brick_count"]
            if archived_validation.get("brick_count") is not None
            else None
        ),
        "matches_archived_native_components": (
            replay_groups == set(map(frozenset, archived_groups))
            if archived_groups is not None
            else None
        ),
        "matches_standard_part_count": column.part_count == standard.part_count
        if standard
        else None,
    }


def _blind(
    column: Column | None = None, analysis: str = "unavailable", bus: dict | None = None
) -> dict:
    sizes = _sizes(column) if column else []
    return {
        "analysis": analysis,
        "part_count": column.part_count if column else None,
        "component_count": len(sizes) if column else None,
        "component_sizes": sizes,
        "largest_component_part_count": sizes[0] if sizes else None,
        "collision_count": len(column.collisions) if column else None,
        "floating_count": len(column.floating) if column else None,
        "contact_only_count": len(column.contact_only_parts) if column else None,
        "contact_only_parts": [
            _neutral_location(column, index)
            for index in column.contact_only_parts[:MAX_BLIND_SAMPLES]
        ]
        if column
        else [],
        "body_chassis_support": bus,
    }


def _attempt(archive: Path, row: dict) -> Path | None:
    value = row.get("attempt_path")
    if not isinstance(value, str):
        return None
    path = Path(value)
    if (
        len(path.parts) != 4
        or path.parts[:3] != ("tasks", row["task_id"], f"trial-{row['trial_index']}")
        or not _ATTEMPT.fullmatch(path.name)
    ):
        raise ValueError("invalid attempt path in collector index")
    selected = archive / path
    if not selected.resolve().is_relative_to(archive.resolve()):
        raise ValueError("attempt path escapes archive")
    return selected


def audit_trial(archive: Path, row: dict, replay: Replay | None = None) -> dict:
    """Retain exact archived evidence and an independently sanitized blind payload."""
    delivered = row.get("delivered")
    native: dict[str, Any] = {"archive": None, "replay": None, "issues": []}
    standard: dict[str, Any] = {"complete": False, "column": None, "issues": []}
    result: dict[str, Any] = {
        "schema_version": 1,
        "task": row["task_id"],
        "trial": row["trial_index"],
        "eligibility": "delivered"
        if delivered is True
        else "not_delivered"
        if delivered is False
        else "unknown",
        "model_sha256": None,
        "native": native,
        "standard": standard,
        "blind": _blind(),
    }
    attempt = _attempt(archive, row)
    if attempt is None:
        standard["issues"].append("no archived attempt path")
        return result
    verifier = attempt / "verifier/score-details.json"
    try:
        details = _read(verifier)
        result["verifier_sha256"] = _sha(verifier)
    except (OSError, ValueError, TypeError) as exc:
        details = {}
        standard["issues"].append(str(exc))
    product = attempt / ARTIFACTS
    model = product / "model.mpd"
    try:
        raw = model.read_bytes()
    except OSError as exc:
        standard["issues"].append(str(exc))
        return result

    model_sha = _digest(raw)
    result["model_sha256"] = model_sha
    if row.get("model_sha256") not in (None, model_sha):
        standard["issues"].append("MPD digest differs from collector index")
    expected = details.get("pieces")
    standard["verifier_part_count"] = expected
    column = None
    bus = None
    try:
        column, bus, issues = _standard_read(raw, row["task_id"])
        standard["column"] = column.model_dump(mode="json")
        standard["issues"].extend(issues)
        if type(expected) is not int or column.part_count != expected:
            standard["issues"].append(
                "standard part count differs from recorded verifier count"
            )
        standard["complete"] = not standard["issues"]
    except (ValueError, RuntimeError, OSError) as exc:
        standard["issues"].append(f"standard inspection failed: {exc}")
    if delivered is True:
        blind_state = "complete" if standard["complete"] else "incomplete"
        result["blind"] = _blind(
            column, blind_state, bus if standard["complete"] else None
        )
    record, authentic, issues = _original_native(product, details, model_sha)
    native.update(archive=authentic, issues=issues)
    if (
        replay is not None
        and record is not None
        and authentic is not None
        and "structure" not in authentic
    ):
        recovered = replay.run(record, row["task_id"], column)
        native["replay"] = _replay_agreement(recovered, authentic, column, model_sha)
    return result


def _discover(archive: Path) -> list[dict]:
    """Discover the final archived attempt when replaying an older unindexed run."""
    rows = []
    for directory in sorted(archive.glob("tasks/R-*/trial-*")):
        task = directory.parent.name
        number = directory.name.removeprefix("trial-")
        if not _TASK.fullmatch(task) or not number.isdecimal():
            continue
        attempts = [
            path
            for path in directory.glob("attempt-*")
            if _ATTEMPT.fullmatch(path.name)
        ]
        if not attempts:
            continue
        final = max(attempts, key=lambda path: int(path.name.split("-")[-1]))
        try:
            result = _read(final / "verifier/score-details.json")
            delivered = (
                result.get("native_finalization") == "pass"
                and result.get("quality") is not None
            )
        except (OSError, ValueError, TypeError):
            delivered = None
        rows.append(
            {
                "task_id": task,
                "trial_index": int(number),
                "delivered": delivered,
                "attempt_path": str(final.relative_to(archive)),
            }
        )
    return rows


def audit_arm(
    archive: Path,
    output: Path,
    *,
    tasks: tuple[str, ...] = TASKS,
    repetitions: int = 2,
    replay: Replay | None = None,
    standard_sdk: dict | None = None,
) -> dict:
    """Audit all requested slots, including rejected or missing deliveries."""
    path = archive / "index.json"
    entries = _read(path)["trials"] if path.is_file() else _discover(archive)
    rows = {}
    for row in entries:
        key = (row["task_id"], row["trial_index"])
        if key in rows:
            raise ValueError(f"duplicate collector trial {key}")
        rows[key] = row
    coverage: Counter[str] = Counter(
        {
            "expected": 0,
            "delivered": 0,
            "not_delivered": 0,
            "unknown": 0,
            "mpds_checked": 0,
            "complete_deliveries": 0,
        }
    )
    sdk = standard_sdk if standard_sdk is not None else sdk_provenance()
    trials = []
    for task in tasks:
        if not _TASK.fullmatch(task):
            raise ValueError(f"invalid task identifier {task}")
        for n in range(1, repetitions + 1):
            row = rows.get((task, n), {"task_id": task, "trial_index": n})
            result = audit_trial(archive, row, replay)
            result["standard_sdk"] = sdk
            relative = Path(task) / f"trial-{n}.json"
            _write(output / relative, result)
            coverage["expected"] += 1
            coverage[result["eligibility"]] += 1
            coverage["mpds_checked"] += result["model_sha256"] is not None
            coverage["complete_deliveries"] += result["blind"]["analysis"] == "complete"
            trials.append(
                {
                    "task": task,
                    "trial": n,
                    "path": relative.as_posix(),
                    "eligibility": result["eligibility"],
                    "model_sha256": result["model_sha256"],
                    "analysis": result["blind"]["analysis"],
                }
            )
    index = {
        "schema_version": 1,
        "standard_sdk": sdk,
        "coverage": dict(coverage),
        "trials": trials,
    }
    _write(output / "index.json", index)
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tasks", nargs="+", default=TASKS)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--frozen-standard-wheel", type=Path)
    parser.add_argument("--replay-package-path", action="append", type=Path, default=[])
    parser.add_argument("--replay-inputs", type=Path)
    parser.add_argument("--replay-ldraw-library", type=Path)
    parser.add_argument("--replay-revision")
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("repetitions must be positive")
    if any(not package.is_dir() for package in args.replay_package_path):
        parser.error("each replay package path must be an extracted package root")
    replay = (
        Replay(
            packages=tuple(args.replay_package_path),
            dataset=args.replay_inputs,
            ldraw_library=args.replay_ldraw_library,
            revision=args.replay_revision,
        )
        if args.replay_package_path
        else None
    )
    result = audit_arm(
        args.archive,
        args.output,
        tasks=tuple(args.tasks),
        repetitions=args.repetitions,
        replay=replay,
        standard_sdk=sdk_provenance(args.frozen_standard_wheel),
    )
    print(json.dumps(result["coverage"], sort_keys=True))


if __name__ == "__main__":
    main()
