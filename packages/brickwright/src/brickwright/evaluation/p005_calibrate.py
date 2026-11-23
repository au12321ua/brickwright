"""Calibrate P-005 against frozen OMR36 target images and archived verifier results."""

import argparse
import hashlib
import json
from importlib.metadata import version
from pathlib import Path

from brickwright._aligned_compare import read_rgba
from brickwright.aligned_review import Cameras, compare_images, render_model

SCORES = ("silhouette_iou", "rgb_agreement_over_union")
PNG_TOLERANCE = 1e-12
REFERENCE_MINIMUM = 0.995
BASELINE_TOLERANCE = 0.005
TASK_IDS = ("R-5521", "R-21011", "R-21020", "R-31038", "R-31039", "R-10258")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def calibrate(dataset: Path, archive: Path, output: Path, library: Path) -> dict:
    import wgpu

    report = {
        "schema_version": 1,
        "thresholds": {
            "archive_png_delta": PNG_TOLERANCE,
            "reference_minimum": REFERENCE_MINIMUM,
            "baseline_rerender_delta": BASELINE_TOLERANCE,
        },
        "versions": {
            name: version(name)
            for name in (
                "ldraw2mesh",
                "pygfx",
                "wgpu",
                "rendercanvas",
                "numpy",
                "pillow",
            )
        },
        "adapter": dict(wgpu.gpu.request_adapter_sync().info),
        "library": str(library.resolve()),
        "library_colors_sha256": digest(library / "LDConfig.ldr"),
        "tasks": {},
        "failures": [],
        "passed": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    for task_id in TASK_IDS:
        public = dataset / "public" / task_id
        task_archive = archive / "tasks" / task_id / "trial-1/attempt-1"
        verifier = task_archive / "verifier"
        archived = json.loads((verifier / "score-details.json").read_text())["quality"][
            "views"
        ]
        cameras_file = public / "cameras.json"
        cameras = Cameras.model_validate_json(cameras_file.read_bytes())
        reference_model = dataset / "author" / task_id / "reference.mpd"
        baseline_model = task_archive / "artifacts/logs/artifacts/model.mpd"
        if set(archived) != set(cameras.views):
            raise ValueError(f"{task_id} has different archive and camera views")
        task_report = {
            "cameras_sha256": digest(cameras_file),
            "reference_mpd_sha256": digest(reference_model),
            "baseline_mpd_sha256": digest(baseline_model),
            "archived_scores_sha256": digest(verifier / "score-details.json"),
            "views": {},
        }
        report["tasks"][task_id] = task_report
        for name in cameras.views:
            target = public / f"goal-{name}.png"
            archived_png = verifier / f"submission-{name}.png"
            metrics, _ = compare_images(read_rgba(target), read_rgba(archived_png))
            row = {
                "target_sha256": digest(target),
                "archive_png_sha256": digest(archived_png),
                "archived": {score: archived[name][score] for score in SCORES},
                "recomputed": {score: metrics[score] for score in SCORES},
            }
            task_report["views"][name] = row
            for score in SCORES:
                if abs(metrics[score] - archived[name][score]) > PNG_TOLERANCE:
                    report["failures"].append([task_id, name, "recomputed", score])
        for label, source in (
            ("reference", reference_model),
            ("baseline", baseline_model),
        ):
            paths = render_model(
                source, cameras, output / task_id / label, library=library
            )
            for name, candidate in paths.items():
                target = read_rgba(public / f"goal-{name}.png")
                metrics, _ = compare_images(target, read_rgba(candidate))
                row = task_report["views"][name]
                row[label] = {score: metrics[score] for score in SCORES}
                row[f"{label}_png_sha256"] = digest(candidate)
                for score in SCORES:
                    value = metrics[score]
                    bad = (
                        value < REFERENCE_MINIMUM
                        if label == "reference"
                        else abs(value - archived[name][score]) > BASELINE_TOLERANCE
                    )
                    if bad:
                        report["failures"].append([task_id, name, label, score])
        print(f"{task_id} completed", flush=True)
    report["passed"] = not report["failures"]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--library", type=Path, required=True)
    args = parser.parse_args()
    report = calibrate(args.dataset, args.archive, args.output, args.library)
    (args.output / "calibration.json").write_text(json.dumps(report, indent=2) + "\n")
    if not report["passed"]:
        raise SystemExit(f"{len(report['failures'])} calibration tolerances exceeded")


if __name__ == "__main__":
    main()
