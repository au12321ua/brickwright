"""Package a fixed-region repair comparison for a blind visual judgment."""

import argparse
import json
import secrets
from pathlib import Path

from PIL import Image, ImageDraw

from brickwright._aligned_compare import compare_images, read_rgba

from .p005_calibrate import SCORES, digest


def repair_evidence(
    before: Path,
    after: Path,
    view: str,
    bbox: tuple[int, int, int, int],
    output: Path,
) -> dict:
    """Compare one reviewer-identified region in two frozen review packets."""
    manifests = [
        json.loads((directory / "manifest.json").read_bytes())
        for directory in (before, after)
    ]
    target_name = f"goal-{view}.png"
    candidate_name = f"candidate-{view}.png"
    for directory, manifest in zip((before, after), manifests, strict=True):
        for name in ("cameras.json", target_name, candidate_name):
            if digest(directory / name) != manifest["files"][name]:
                raise ValueError(f"frozen review file changed: {directory / name}")
    for key in ("cameras.json", target_name):
        if manifests[0]["files"][key] != manifests[1]["files"][key]:
            raise ValueError("before and after must use the same camera and target")
    if manifests[0]["model_sha256"] == manifests[1]["model_sha256"]:
        raise ValueError("before and after must be different models")

    target = read_rgba(before / target_name)
    left, top, right, bottom = bbox
    if not (
        0 <= left < right <= target.shape[1] and 0 <= top < bottom <= target.shape[0]
    ):
        raise ValueError("region is outside the selected image")
    outputs = output / "private"
    outputs.mkdir(parents=True, exist_ok=True)
    pixels = target[top:bottom, left:right]
    representative = {"before": before, "after": after}
    scores, images = {}, {}
    for name, folder in representative.items():
        image = read_rgba(folder / candidate_name)
        if target.shape != image.shape:
            raise ValueError("candidate images must have the target image dimensions")
        crop = image[top:bottom, left:right]
        images[name] = Image.fromarray(crop)
        metrics, overlay = compare_images(pixels, crop)
        Image.fromarray(overlay).save(outputs / f"{name}-overlay.png")
        scores[name] = {metric: metrics[metric] for metric in SCORES}
    order = list(representative)
    secrets.SystemRandom().shuffle(order)
    labels = {
        "Target": Image.fromarray(pixels),
        "A": images[order[0]],
        "B": images[order[1]],
    }
    width, height = right - left, bottom - top
    tile_width = max(90, width) + 12
    sheet = Image.new("RGB", (tile_width * len(labels), height + 30), (232, 232, 232))
    draw = ImageDraw.Draw(sheet)
    for index, (label, image) in enumerate(labels.items()):
        x = index * tile_width + 6
        draw.text((x, 5), label, fill=(25, 25, 25))
        sheet.paste(image, (x, 22), image.getchannel("A"))
    sheet.save(output / "blind.png")
    report = {
        "schema_version": 1,
        "view": view,
        "bbox": list(bbox),
        "scores": scores,
        "delta": {
            metric: scores["after"][metric] - scores["before"][metric]
            for metric in SCORES
        },
        "rounds": {
            name: manifest["round_id"]
            for name, manifest in zip(representative, manifests, strict=True)
        },
        "image_order": dict(zip(("A", "B"), order, strict=True)),
    }
    (outputs / "local-metrics.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path, help="before round's inputs directory")
    parser.add_argument("after", type=Path, help="after round's inputs directory")
    parser.add_argument("view")
    parser.add_argument("output", type=Path)
    parser.add_argument("--bbox", nargs=4, type=int, required=True)
    args = parser.parse_args()
    left, top, right, bottom = args.bbox
    repair_evidence(
        args.before, args.after, args.view, (left, top, right, bottom), args.output
    )


if __name__ == "__main__":
    main()
