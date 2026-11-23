"""Prepare anonymous OMR36 image packets and evaluate sealed trial scores."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import secrets
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
from typing import Any

from PIL import Image

TASKS = ("R-5521", "R-21011", "R-21020", "R-31038", "R-31039", "R-10258")
VIEWS = ("angle", "face", "end", "top", "reverse")
REPETITIONS = 2


def _read(path: Path) -> Any:
    return json.loads(path.read_bytes())


def _write(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _new_packet(output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"Packet already exists: {output}")
    with TemporaryDirectory(dir=output.parent) as temporary:
        staging = Path(temporary) / "packet"
        (staging / "review").mkdir(parents=True)
        (staging / "private").mkdir(mode=0o700)
        yield staging
        staging.rename(output)


def _image(source: Path, destination: Path) -> None:
    with Image.open(source) as image:
        pixels = image.convert("RGBA")
        pixels.info.clear()
        pixels.save(destination)


def _archive_report(root: Path, task: str, trial: int) -> Path:
    index_path = root / "index.json"
    folder = root / "tasks" / task / f"trial-{trial}"
    if index_path.is_file():
        index = _read(index_path)
        rows = [
            r
            for r in index["trials"]
            if r["task_id"] == task and r["trial_index"] == trial
        ]
        if len(rows) != 1 or rows[0].get("state") != "scored":
            raise ValueError(
                f"No unique scored platform verdict for {task} trial {trial}"
            )
        row = rows[0]
        attempt = root / row["attempt_path"]
        if attempt.resolve().parent != folder.resolve():
            raise ValueError("Selected trial does not match its archive path.")
        report = attempt / "verifier" / "score-details.json"
        if (
            _digest(report) != row.get("score_details_sha256")
            or _read(report)["score"] != row["score"]
        ):
            raise ValueError(
                "Platform index does not match the selected verifier score."
            )
        return report
    attempts = sorted(
        folder.glob("attempt-*"), key=lambda path: int(path.name.split("-")[-1])
    )
    if not attempts:
        raise ValueError(f"No scored attempt at {folder}")
    # Maestro retains retry attempts. Only the final attempt represents this trial.
    report = attempts[-1] / "verifier" / "score-details.json"
    if not report.is_file():
        raise ValueError(f"Missing platform score at {report}")
    return report


def _platform_result(data: dict) -> tuple[bool, float]:
    score = data.get("score")
    if (
        isinstance(score, bool)
        or not isinstance(score, (float, int))
        or not math.isfinite(score)
        or not 0 <= score <= 1
    ):
        raise ValueError("A finished verifier score in [0, 1] is required.")
    if data.get("delivery") == "invalid" and score == 0:
        return False, 0.0
    if not (
        data.get("delivery") == "present"
        and data.get("native_finalization") == "pass"
        and data.get("fixed_and_inventory") == "pass"
        and set((data.get("quality") or {}).get("views", {})) == set(VIEWS)
    ):
        raise ValueError("Incomplete delivery or view evidence from the verifier.")
    return True, float(score)


def _fig(path: str, label: str) -> str:
    return f'<figure><figcaption>{html.escape(label)}</figcaption><a href="{path}"><img loading="lazy" src="{path}" alt="{html.escape(label)}"></a></figure>'


def _page(body: str) -> str:
    return (
        '<!doctype html><html lang="zh"><meta charset="utf-8"><title>OMR36 盲评</title>'
        "<style>body{font:16px system-ui;margin:2rem auto;max-width:95vw}figure{display:inline-block;width:31%;margin:.4%}img{width:100%;background:#eee}section{border-top:1px solid #999;padding-top:1rem;margin-top:2rem}</style><main>"
        + body
        + "</main></html>"
    )


def _seal(packet: Path, key: dict) -> None:
    review = packet / "review"
    key["files"] = {
        str(path.relative_to(review)): _digest(path)
        for path in sorted(review.rglob("*"))
        if path.is_file() and path.name != "scores.json"
    }
    _write(packet / "private" / "key.json", key)


def prepare(
    inputs: Path,
    baseline: Path,
    candidate: Path,
    output: Path,
    bus_modules: Path | None = None,
    structure: Path | None = None,
) -> None:
    """Create one anonymous item per trial. Keep the key outside the review directory."""
    if baseline.resolve() == candidate.resolve():
        raise ValueError("Separate baseline and candidate archives are required.")
    arms = {"baseline": (baseline, TASKS), "candidate": (candidate, TASKS)}
    if bus_modules:
        arms["bus_modules"] = (bus_modules, ("R-10258",))
    with _new_packet(output) as packet:
        review = packet / "review"
        key: dict[str, dict] = {}
        sheets: dict[str, list[dict]] = {}
        cards: list[tuple[str, str]] = []
        for arm, (archive, tasks) in arms.items():
            for task in tasks:
                input_dir = inputs / task / "environment" / "input"
                source = input_dir / "task.json"
                original = _read(source)
                conditions = original["checks"]
                if (
                    original["id"] != task
                    or set(original["views"]) != set(VIEWS)
                    or len(conditions) != 3
                    or not all(isinstance(c, str) and c.strip() for c in conditions)
                ):
                    raise ValueError(
                        f"Original checks/views for {task} differ from frozen OMR36."
                    )
                target = review / "targets" / task
                if not target.exists():
                    target.mkdir(parents=True)
                    for view in VIEWS:
                        _image(input_dir / f"goal-{view}.png", target / f"{view}.png")
                for n in range(1, REPETITIONS + 1):
                    report = _archive_report(archive, task, n)
                    result = _read(report)
                    passed, platform_score = _platform_result(result)
                    ident = secrets.token_hex(6)
                    folder = review / ident
                    folder.mkdir()
                    key[ident] = {
                        "arm": arm,
                        "trial": n,
                        "task": task,
                        "conditions": conditions,
                        "delivery": passed,
                        "score": platform_score,
                        "task_sha256": _digest(source),
                        "verifier_sha256": _digest(report),
                    }
                    sheets[ident] = [
                        {"condition": c, "result": None, "evidence": ""}
                        for c in conditions
                    ]
                    views = []
                    for view in VIEWS:
                        source_image = report.parent / f"submission-{view}.png"
                        if not passed or not source_image.is_file():
                            if passed:
                                raise ValueError(
                                    f"Missing verifier image: {source_image}"
                                )
                            continue
                        _image(source_image, folder / f"model-{view}.png")
                        views.append(
                            "<div>"
                            + _fig(f"targets/{task}/{view}.png", f"目标 {view}")
                            + _fig(f"{ident}/model-{view}.png", f"待评 {view}")
                            + "</div>"
                        )
                    body = "".join(views) or "<p>无通过交付检查的成品图。</p>"
                    if structure is not None:
                        label = "bus-modular" if arm == "bus_modules" else arm
                        sidecar = structure / label / task / f"trial-{n}.json"
                        neutral = _read(sidecar)["blind"]
                        _write(folder / "structure.json", neutral)
                        key[ident]["structure_source_sha256"] = _digest(sidecar)
                        body += f'<p><a href="{ident}/structure.json">标准目录结构复查</a></p>'
                    checks = "".join(f"<li>{html.escape(c)}</li>" for c in conditions)
                    cards.append(
                        (
                            ident,
                            f"<section><h2>{task} · {ident}</h2><ol>{checks}</ol>{body}</section>",
                        )
                    )
        # Random identifiers also order both the page and rubric. Arm insertion order would reveal version.
        (review / "index.html").write_text(
            _page(
                "<h1>原始完成条件盲评</h1><p>点击图像可看原尺寸。在 scores.json 中逐条填 met、unmet 或 insufficient，并填写机位、位置或结构依据。</p>"
                + "".join(card for _, card in sorted(cards))
            )
        )
        _write(review / "scores.json", {k: sheets[k] for k in sorted(sheets)})
        _seal(packet, {"trials": key})


def _validated_key(packet: Path) -> dict:
    key = _read(packet / "private" / "key.json")
    for name, expected in key["files"].items():
        if _digest(packet / "review" / name) != expected:
            raise ValueError(f"Blind evidence changed: {name}")
    return key


def score(packet: Path) -> dict:
    trials = _validated_key(packet)["trials"]
    rubric = _read(packet / "review" / "scores.json")
    if set(trials) != set(rubric):
        raise ValueError("The score sheet no longer matches the sealed trial IDs.")
    rows: list[dict[str, Any]] = []
    for ident, item in trials.items():
        marks = rubric[ident]
        if [mark["condition"] for mark in marks] != item["conditions"]:
            raise ValueError(f"Original conditions changed: {ident}")
        counts = defaultdict(int)
        for mark in marks:
            verdict = mark["result"]
            if verdict is None:
                counts["pending"] += 1
            elif (
                verdict in {"met", "unmet", "insufficient"}
                and isinstance(mark.get("evidence"), str)
                and mark["evidence"].strip()
            ):
                if verdict == "met" and not item["delivery"]:
                    raise ValueError(f"No verified model for a met condition: {ident}")
                counts[verdict] += 1
            else:
                raise ValueError(f"Expected verdict and location/evidence for {ident}")
        rows.append({**item, "id": ident, "verdicts": marks, "counts": dict(counts)})

    def summary(selected):
        return {
            "trials": len(selected),
            "delivery_pass_rate": mean(r["delivery"] for r in selected),
            "platform_score_mean": mean(r["score"] for r in selected),
            "conditions_met": sum(r["counts"].get("met", 0) for r in selected),
            "pending": sum(r["counts"].get("pending", 0) for r in selected),
        }

    arms = {
        arm: summary([r for r in rows if r["arm"] == arm])
        for arm in sorted({r["arm"] for r in rows})
    }
    old, new = arms["baseline"], arms["candidate"]
    criteria = {
        field: new[field] >= old[field]
        for field in ("delivery_pass_rate", "platform_score_mean", "conditions_met")
    }
    status = (
        "pending"
        if old["pending"] or new["pending"]
        else "pass"
        if all(criteria.values())
        else "fail"
    )
    return {"status": status, "criteria": criteria, "arms": arms, "trials": rows}


def prepare_jet(goals: Path, before: Path, after: Path, output: Path) -> None:
    """Compare two directories of named fixed-camera PNGs without exposing version order."""
    if before.resolve() == after.resolve():
        raise ValueError("Separate before and after image sets are required.")
    versions = [("before", before), ("after", after)]
    secrets.SystemRandom().shuffle(versions)
    with _new_packet(output) as packet:
        review = packet / "review"
        key = {}
        body = "<h1>喷气机同机位局部盲评</h1><p>在 scores.json 中填 A、B、tie 或 insufficient，并写明部位、机位和可见差异。请按目标图判定。</p>"
        for view in VIEWS:
            _image(goals / f"goal-{view}.png", review / f"goal-{view}.png")
            body += "<div>" + _fig(f"goal-{view}.png", f"目标 {view}")
            for letter, (version, folder) in zip("AB", versions, strict=True):
                key[letter] = version
                _image(folder / f"{view}.png", review / f"{letter}-{view}.png")
                body += _fig(f"{letter}-{view}.png", f"{letter} {view}")
            body += "</div>"
        (review / "index.html").write_text(_page(body))
        _write(
            review / "scores.json",
            {
                "preferred": None,
                "region": "",
                "view": "",
                "corroborating_view": "",
                "evidence": "",
            },
        )
        _seal(packet, {"versions": key})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("prepare")
    for name in ("inputs", "baseline", "candidate", "output"):
        new.add_argument("--" + name, type=Path, required=True)
    new.add_argument("--bus-modules", type=Path)
    new.add_argument(
        "--structure",
        type=Path,
        help="common offline structure analysis for each sealed platform trial",
    )
    jet = commands.add_parser("jet")
    for name in ("goals", "before", "after", "output"):
        jet.add_argument("--" + name, type=Path, required=True)
    mark = commands.add_parser("score")
    mark.add_argument("packet", type=Path)
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "prepare":
        prepare(**args)
    elif command == "jet":
        prepare_jet(**args)
    else:
        print(json.dumps(score(**args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
