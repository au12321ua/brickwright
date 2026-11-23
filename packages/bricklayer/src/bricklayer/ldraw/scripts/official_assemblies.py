"""Check the SDK against independently sourced official assembly poses."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from math import acos, degrees
from pathlib import Path
from subprocess import run
from typing import Any

from ...diagnostics import connector_evidence
from ...geometry.connections import connectors_mate
from ...geometry.spatial import Pose
from ...modeling.assembly import AssemblyError, ConnectionCandidate
from ...modeling.build import LegoBuild
from ...modeling.placement import Placement
from ...parts.element import Element
from ...parts.official import lookup_part
from ..colors import parse_color_code
from ..conversion import LDrawVec, pose_to_ldraw
from ..meshes import DATA_DIR, MeshUnavailable
from .official_assembly_inventory import (
    INVENTORY_RULE_VERSION,
    CoveredInterface,
    Instance,
    SourceLine,
    read_assembly,
    scan_inventory,
)
from .official_snapshot import Snapshot, scan_snapshot

_RELEASE = "2026-10"
_QUERY_LIMIT = 20
_TRIAL_LIMIT = 8
_TRANSLATION_TOLERANCE = 0.1
_ROTATION_TOLERANCE = 0.1
_WHEEL_RULE = "wheel-1"
_WHEEL_MANIFEST = "81cb98b4ba602799af999c1e9c3357986cbb7b5a8ecc4e7c8d1651b3ceecf34e"
_WHEEL_PAIRS = frozenset(
    (f"fixed_{subtype}_{first}", f"fixed_{subtype}_{second}")
    for subtype in range(1, 32)
    for first, second in (("in", "on"), ("on", "in"))
)


def rotation_error(reference: Pose, actual: Pose) -> float:
    """The angle in degrees between the two full, signed rotations."""
    change = reference.rotation.transpose @ actual.rotation
    cosine = (float(change.array.trace()) - 1) / 2
    return degrees(acos(max(-1, min(1, cosine))))


@dataclass(frozen=True)
class Negative:
    name: str
    pose: Pose
    evidence: str


@dataclass(frozen=True)
class Sample:
    sample_id: str
    host: Instance
    child: Instance
    expected_pairs: frozenset[tuple[str, str]]
    negative: Pose | None
    negative_evidence: str
    family: str
    equivalence: Callable[[Pose, Pose], float] = rotation_error
    additional_negatives: tuple[Negative, ...] = ()
    positive_evidence: str = ""


def _shift_local(pose: Pose, x: float, y: float, z: float) -> Pose:
    return pose.compose(Pose(translation=LDrawVec(x, y, z).to_position()))


def wheel_samples(root: Path, snapshot: Snapshot) -> tuple[Sample, ...]:
    """Apply the original two-direct-instance selection to the frozen library."""
    samples = []
    for source in sorted(snapshot.parts):
        # Every selected source in the freeze also carries the Shortcut flag.
        # The name-based selection includes non-Shortcut rows if the freeze changes.
        row = snapshot.parts[source]
        if not row["name"].startswith("Wheel"):
            continue
        assembly = read_assembly(root, source, snapshot)
        if len(assembly.root_lines) != 2 or len(assembly.direct) != 2:
            continue
        host, child = sorted(
            assembly.direct,
            key=lambda p: snapshot.parts[p.part_id]["name"].startswith("Tyre"),
        )
        if not snapshot.parts[host.part_id]["name"].startswith("Wheel"):
            continue
        if not snapshot.parts[child.part_id]["name"].startswith("Tyre"):
            continue
        extra = _wheel_axial_negative(host, child)
        samples.append(
            Sample(
                f"{source}/wheel",
                host,
                child,
                _WHEEL_PAIRS,
                _shift_local(child.pose, 1, 1, 0),
                "The tyre bead and rim must share an axis. A 1 LDU shift along both transverse dimensions breaks the fit.",
                "wheel",
                additional_negatives=extra,
                positive_evidence="The official two-component Shortcut puts the tyre bead over the rim's retaining collar.",
            )
        )
    return tuple(samples)


def _wheel_axial_negative(host: Instance, child: Instance) -> tuple[Negative, ...]:
    # The collar of 497a (within 7039) is centered at -2, between the -8
    # and +4 edges of its radius-19 seat. The -2.33335 location is the
    # source label's displaced seat and leaves the bead off its center.
    if host.part_id == "7039":
        return (
            Negative(
                "axial_bead_displacement",
                _shift_local(child.pose, 0, 0, -0.33335),
                "497a.dat centers the radius-19 seating cylinder midway between -8 and +4 LDU. Both tyre designs have centered beads.",
            ),
        )
    if host.part_id == "56904":
        return (
            Negative(
                "axial_bead_displacement",
                _shift_local(child.pose, 0, 0, -2),
                "The matched radius-38 shoulders in 56904s00.dat and the tyre are centered on 0. A -2 LDU shift moves the tyre into a retaining shoulder.",
            ),
        )
    return ()


def _pose(pose: Pose) -> list[float]:
    matrix, t = pose_to_ldraw(pose)
    return [*t.tolist(), *matrix.flatten().tolist()]


def _error(
    reference: Pose, actual: Pose, equivalence: Callable[[Pose, Pose], float]
) -> dict[str, float | bool]:
    shift = LDrawVec.from_position(actual.translation - reference.translation).norm()
    angle = equivalence(reference, actual)
    return {
        "translation_ldu": float(shift),
        "rotation_deg": angle,
        "equivalent": shift <= _TRANSLATION_TOLERANCE and angle <= _ROTATION_TOLERANCE,
    }


def _type_pair(candidate: ConnectionCandidate) -> tuple[str, str]:
    return candidate.parent.connector.type.name, candidate.child.connector.type.name


def _direct(
    host: Placement[Element],
    child: Placement[Element],
    child_pose: Pose,
    expected: frozenset[tuple[str, str]],
) -> dict[str, Any]:
    model = LegoBuild()
    first = model.add_raw(host.content, host.pose, name="host")
    second = model.add_raw(child.content, child_pose, name="child")
    contacts = [
        [connector_evidence(a), connector_evidence(b)]
        for a in first.connectors
        for b in second.connectors
        if connectors_mate(a, b)
    ]
    contacts.sort(key=lambda p: json.dumps(p, sort_keys=True))
    intended = any((a["type"], b["type"]) in expected for a, b in contacts)
    collisions = [
        [p.content.ldraw_id for p in pair] for pair in model.colliding_placements
    ]
    return {
        "connected": model.is_connected(),
        "expected_interface": intended,
        "collision_free": not collisions,
        "passed": intended and not collisions,
        "matched_endpoints": contacts,
        "connector_position_units": ["stud", "stud", "brick"],
        "collision_pairs": collisions,
    }


def _recover(
    model: LegoBuild,
    candidates: tuple[ConnectionCandidate, ...],
    reference: Pose,
    host_inverse: Pose,
    element_to_source: Pose,
    sample: Sample,
) -> dict[str, Any]:
    trials: list[dict[str, Any]] = []
    for rank, trial in enumerate(model.trial_connections(candidates), start=1):
        candidate = trial.candidate
        fitted = (
            None
            if candidate.pose is None
            else host_inverse.compose(candidate.pose).compose(element_to_source)
        )
        error = (
            None if fitted is None else _error(reference, fitted, sample.equivalence)
        )
        trials.append(
            {
                "rank": rank,
                "endpoints": _type_pair(candidate),
                "checks": trial.checks,
                "diagnostics": [
                    {"code": d.code, "message": str(d), "evidence": d.evidence}
                    for d in trial.diagnostics
                ],
                "passed_trial": trial.passed,
                "relative_ldraw": None if fitted is None else _pose(fitted),
                "errors": error,
                "passed": bool(
                    trial.passed
                    and _type_pair(candidate) in sample.expected_pairs
                    and error is not None
                    and error["equivalent"]
                ),
            }
        )
    first = next((trial["rank"] for trial in trials if trial["passed"]), None)
    any_passed = any(trial["passed_trial"] for trial in trials)
    if first is not None:
        status = "equivalent"
    elif any_passed:
        status = "non_equivalent_pass"
    elif not candidates:
        status = "zero_candidates"
    elif all(
        any(d["code"] == "assembly.collision" for d in trial["diagnostics"])
        for trial in trials
    ):
        status = "all_trials_collide"
    else:
        status = "all_trials_failed"
    return {
        "candidate_count": len(candidates),
        "tried": len(trials),
        "untried": len(candidates) - len(trials),
        "first_equivalent_rank": first,
        "any_trial_passed": any_passed,
        "status": status,
        "passed": first is not None,
        "trials": trials,
    }


def _negative(
    negative: Negative,
    sample: Sample,
    host: Placement[Element],
    child: Placement[Element],
    model: LegoBuild,
    candidates: tuple[ConnectionCandidate, ...],
    element_to_source: Pose,
) -> dict[str, Any]:
    wrong_element = negative.pose.compose(element_to_source.inverse)
    direct = _direct(host, child, wrong_element, sample.expected_pairs)
    host_inverse = sample.host.pose.inverse
    reference = sample.child.relative_to(sample.host)
    commits = []
    for rank, candidate in enumerate(candidates[:_TRIAL_LIMIT], start=1):
        # target_position is the requested world position of the source
        # connector, so the SDK may snap a fixed or hinge joint into its seat.
        requested = replace(
            candidate,
            target_position=wrong_element.apply(candidate.child.connector.position),
            target_rotation=wrong_element.rotation,
        )
        trial_model = model.copy()
        try:
            placed = trial_model.place_connection(requested)
        except AssemblyError as exc:
            commits.append(
                {
                    "rank": rank,
                    "status": "rejected",
                    "checks": exc.trial.checks,
                    "code": exc.code,
                    "evidence": exc.evidence,
                }
            )
            continue
        fitted = placed.pose.compose(element_to_source)
        expected_pair = _type_pair(candidate) in sample.expected_pairs
        if _error(negative.pose, fitted, rotation_error)["equivalent"]:
            status = "false_fit"
        elif (
            expected_pair
            and _error(reference, host_inverse.compose(fitted), sample.equivalence)[
                "equivalent"
            ]
        ):
            status = "corrected_to_reference"
        else:
            status = "different_pose"
        commits.append(
            {
                "rank": rank,
                "status": status,
                "endpoints": _type_pair(candidate),
                "actual_ldraw": _pose(fitted),
            }
        )
    # Any collision-free connection between these two parts is unintended
    # when the designated reference pose has been displaced off its seat.
    false_fit = (direct["connected"] and direct["collision_free"]) or any(
        row["status"] == "false_fit" for row in commits
    )
    return {
        "name": negative.name,
        "evidence": negative.evidence,
        "requested_ldraw": _pose(negative.pose),
        "direct": direct,
        "commits": commits,
        "passed": not false_fit,
    }


def evaluate(sample: Sample, root: Path) -> dict[str, Any]:
    """Run the three independent predicates for a specific pair of instances."""
    row: dict[str, Any] = {
        "sample_id": sample.sample_id,
        "family": sample.family,
        "source": sample.host.path[0].source,
        "positive_evidence": sample.positive_evidence,
        "canonical_ordered_pair": [
            lookup_part(item.part_id).canonical_id
            for item in (sample.host, sample.child)
        ],
        "host": asdict(sample.host),
        "child": asdict(sample.child),
        "relative_ldraw": None,
    }
    try:
        row["relative_ldraw"] = _pose(sample.child.relative_to(sample.host))
        host = sample.host.placement(root)
        child = sample.child.placement(root)
        return (
            row
            | {
                "parts": [
                    _part_sources(item.content.ldraw_id) for item in (host, child)
                ],
            }
            | _evaluate_placements(sample, host, child)
        )
    except (ValueError, MeshUnavailable) as exc:
        return row | {"unverified": {"type": type(exc).__name__, "reason": str(exc)}}


def _part_sources(part_id: str) -> dict[str, Any]:
    part = lookup_part(part_id)
    return {
        "sdk_id": part_id,
        "collision": asdict(part.collision),
        "connectivity": asdict(part.connectivity),
    }


def _evaluate_placements(
    sample: Sample, host: Placement[Element], child: Placement[Element]
) -> dict[str, Any]:
    direct = _direct(host, child, child.pose, sample.expected_pairs)
    model = LegoBuild()
    first = model.add_raw(host.content, host.pose, name="host")
    candidates = model.find_connection_candidates(
        first, content=child.content, target_rotation=child.pose.rotation
    )
    element_to_source = child.pose.inverse.compose(sample.child.pose)
    recovery = _recover(
        model,
        candidates,
        sample.child.relative_to(sample.host),
        sample.host.pose.inverse,
        element_to_source,
        sample,
    )
    negatives = sample.additional_negatives
    if sample.negative is not None:
        negatives = (
            Negative("wrong_pose", sample.negative, sample.negative_evidence),
            *negatives,
        )
    return {
        "direct": direct,
        "recovery": recovery,
        "negatives": [
            _negative(
                negative, sample, host, child, model, candidates, element_to_source
            )
            for negative in negatives
        ],
    }


def _bus_wheel_samples(path: Path) -> Iterable[Sample]:
    """Read the R-10258 application control from its archived model."""
    data = path.read_bytes()
    digest = sha256(data).hexdigest()
    rims: dict[tuple[float, ...], Instance] = {}
    tyres: dict[tuple[float, ...], Instance] = {}
    for number, raw in enumerate(data.decode("utf-8-sig").splitlines(), start=1):
        fields = raw.split(maxsplit=14)
        if fields[:1] != ["1"] or len(fields) != 15:
            continue
        part_id = fields[14].removesuffix(".dat")
        if part_id not in {"56904", "30699"}:
            continue
        transform = tuple(map(float, fields[2:14]))
        instances = rims if part_id == "56904" else tyres
        if transform in instances:
            raise ValueError("The bus control repeats a wheel instance at one pose")
        line = SourceLine("R-10258/model.mpd", number, raw, digest, "", "")
        instances[transform] = Instance(
            part_id, parse_color_code(fields[1]), transform, (line,)
        )
    if len(rims) != 4 or rims.keys() != tyres.keys():
        raise ValueError(
            "Expected four independently positioned 56904/30699 wheel pairs in the bus control"
        )
    for transform, host in rims.items():
        child = tyres[transform]
        yield Sample(
            f"R-10258/wheel-{host.path[0].line_number}",
            host,
            child,
            _WHEEL_PAIRS,
            _shift_local(child.pose, 1, 1, 0),
            "The tyre bead and rim must share an axis.",
            "bus_application",
            additional_negatives=_wheel_axial_negative(host, child),
            positive_evidence="The archived bus MPD has the rim and tyre at coincident LDraw origins with matching axes.",
        )


def check_wheel_manifest(samples: Iterable[Sample]) -> dict[str, Any]:
    samples = tuple(samples)
    filenames = sorted(
        Path(sample.host.path[0].source).name.lower() for sample in samples
    )
    manifest = sha256(
        "".join(f"{filename}\n" for filename in filenames).encode()
    ).hexdigest()
    unique = len({(sample.host.part_id, sample.child.part_id) for sample in samples})
    if (len(samples), unique, manifest) != (101, 98, _WHEEL_MANIFEST):
        raise ValueError(
            f"Wheel sample selection changed: {len(samples)} files, {unique} pairs, {manifest}"
        )
    return {"files": len(samples), "ordered_part_pairs": unique, "sha256": manifest}


def _status(passed: bool) -> str:
    return "passed" if passed else "failed"


def _predicates(row: dict[str, Any]) -> tuple[str, str, str]:
    if "unverified" in row:
        return "unverified", "unverified", "unverified"
    negatives = row["negatives"]
    return (
        _status(row["direct"]["passed"]),
        _status(row["recovery"]["passed"]),
        _status(all(item["passed"] for item in negatives))
        if negatives
        else "unverified",
    )


def _predicate_counts(statuses: Iterable[str]) -> dict[str, int]:
    counts = Counter(statuses)
    return {status: counts[status] for status in ("passed", "failed", "unverified")}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return counts that use the same sample denominator for each predicate."""
    summary = {}
    for family in sorted({row["family"] for row in rows}):
        members = [row for row in rows if row["family"] == family]
        measured = [row for row in members if "unverified" not in row]
        negatives = [negative for row in measured for negative in row["negatives"]]
        direct, recovery, negative = zip(*map(_predicates, members))
        summary[family] = {
            "source_count": len({row["source"] for row in members}),
            "sample_count": len(members),
            "ordered_part_pairs": len(
                {(row["host"]["part_id"], row["child"]["part_id"]) for row in members}
            ),
            "direct": _predicate_counts(direct),
            "recovery": _predicate_counts(recovery),
            "negative": _predicate_counts(negative),
            "negatives": len(negatives),
            "false_fits": sum(not item["passed"] for item in negatives),
            "direct_legacy": Counter(
                f"connected={row['direct']['connected']}, collision={not row['direct']['collision_free']}"
                for row in measured
            ),
            "recovery_status": Counter(row["recovery"]["status"] for row in measured),
            "any_trial_passed": sum(
                row["recovery"]["any_trial_passed"] for row in measured
            ),
        }
    return summary


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare equal sample sets, including every negative and case-level change."""
    old = {row["sample_id"]: row for row in before["samples"]}
    new = {row["sample_id"]: row for row in after["samples"]}
    if old.keys() != new.keys():
        raise ValueError(
            f"Different sample sets: removed {sorted(old.keys() - new.keys())}, added {sorted(new.keys() - old.keys())}"
        )
    changed = []
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for sample_id in sorted(old):
        left, right = old[sample_id], new[sample_id]
        for key in ("relative_ldraw", "host", "child"):
            if left[key] != right[key]:
                raise ValueError(f"Reference changed for {sample_id}: {key}")
        a, b = _predicates(left), _predicates(right)
        if a != b:
            changed.append({"sample_id": sample_id, "before": a, "after": b})
        for label, start, end in zip(("direct", "recovery", "negative"), a, b):
            counts[label][f"{start} -> {end}"] += 1
        old_negatives = {n["name"]: n for n in left.get("negatives", [])}
        new_negatives = {n["name"]: n for n in right.get("negatives", [])}
        if old_negatives.keys() != new_negatives.keys():
            raise ValueError(f"Negative sample set changed for {sample_id}")
        for name, item in old_negatives.items():
            updated = new_negatives[name]
            if item["requested_ldraw"] != updated["requested_ldraw"]:
                raise ValueError(f"Negative pose changed for {sample_id}: {name}")
            counts["individual_negatives"][
                f"{_status(item['passed'])} -> {_status(updated['passed'])}"
            ] += 1
    return {"changed": changed, "predicates": dict(counts)}


def _repository_revision() -> str | None:
    revision = run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
        check=False,
    )
    return revision.stdout.strip() if revision.returncode == 0 else None


def _data_fingerprints() -> dict[str, str]:
    files = [*DATA_DIR.glob("*.json"), *(DATA_DIR / "meshes").glob("*")]
    return {
        path.relative_to(DATA_DIR).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(files)
        if path.is_file()
    }


def _write(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--ldraw", type=Path, required=True)
    check.add_argument("--family", choices=("wheel", "hinge", "all"), default="all")
    check.add_argument("--output", type=Path, required=True)
    check.add_argument("--inventory", type=Path)
    check.add_argument(
        "--bus-mpd",
        type=Path,
        help="archived R-10258 model for the application control",
    )
    diff = sub.add_parser("diff")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    diff.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "diff":
        _write(
            args.output,
            compare(
                json.loads(args.before.read_text()), json.loads(args.after.read_text())
            ),
        )
        return 0

    root = args.ldraw
    snapshot = scan_snapshot(root)
    snapshot.verify()
    samples: list[Sample] = []
    metadata: dict[str, Any] = {
        "command": argv,
        "release": _RELEASE,
        "source": snapshot.provenance(),
        "sdk_revision": _repository_revision(),
        "data_sha256": _data_fingerprints(),
        "query_limit": _QUERY_LIMIT,
        "trial_limit": _TRIAL_LIMIT,
        "translation_tolerance_ldu": _TRANSLATION_TOLERANCE,
        "rotation_tolerance_deg": _ROTATION_TOLERANCE,
        "rules": {},
    }
    if args.family in {"wheel", "all"}:
        wheel = wheel_samples(root, snapshot)
        metadata["wheel_manifest"] = check_wheel_manifest(wheel)
        metadata["rules"]["wheel"] = _WHEEL_RULE
        samples.extend(wheel)
    if args.family in {"hinge", "all"}:
        from .official_assembly_hinges import HINGE_RULE_VERSION, hinge_samples

        samples.extend(hinge_samples(root, snapshot))
        metadata["rules"]["hinge"] = HINGE_RULE_VERSION
    if args.bus_mpd is not None:
        samples.extend(_bus_wheel_samples(args.bus_mpd))
        metadata["rules"]["bus_application"] = _WHEEL_RULE
    if args.inventory is not None:
        metadata["inventory_rule"] = INVENTORY_RULE_VERSION
    rows = [evaluate(sample, root) for sample in samples]
    _write(
        args.output, {"metadata": metadata, "summary": summarize(rows), "samples": rows}
    )
    if args.inventory is not None:
        coverage = defaultdict(list)
        for sample in samples:
            if sample.family == "bus_application":
                continue
            source = Path(sample.host.path[0].source).stem
            coverage[source].append(
                CoveredInterface(
                    sample.sample_id, sample.family, sample.host, sample.child
                )
            )
        _write(args.inventory, scan_inventory(root, snapshot, coverage))
    return 0


if __name__ == "__main__":
    from sys import argv

    raise SystemExit(main(argv[1:]))
