"""Build a standalone jet replay fixture from the archived final record."""

import argparse
import json
import shutil
from pathlib import Path

from .p005_calibrate import digest

FEATURES = {
    "schema_version": 1,
    "features": [
        {
            "id": "F01",
            "name": "双翼后掠",
            "description": "双翼翼尖在俯视图中均位于翼根后方，保留图中的整体后掠轮廓。",
            "category": "retain",
            "importance": "required",
            "evidence": [{"check_id": "C01"}],
            "rationale": "保留喷气机双翼的识别轮廓。",
        },
        {
            "id": "F02",
            "name": "翼根接机身",
            "description": "双翼在图示位置接入机身，保留翼根与机身的相对几何关系。",
            "category": "retain",
            "importance": "required",
            "evidence": [{"check_id": "C02"}],
            "rationale": "保留图示的机翼与机身关系。",
        },
        {
            "id": "F03",
            "name": "起落架位置",
            "description": "前后轮和两侧主轮相对机腹的位置与题图一致。",
            "category": "retain",
            "importance": "required",
            "evidence": [{"check_id": "C03"}],
            "rationale": "保留图示的停机姿态。",
        },
    ],
}


def prepare(dataset: Path, archive: Path, output: Path) -> None:
    public = dataset / "public" / "R-31039"
    baseline = archive / "tasks/R-31039/trial-1/attempt-1/artifacts/logs/artifacts"
    model = baseline / "model.mpd"
    records = [
        path.parent.parent
        for path in (baseline / "records").glob("*/out/build.ldr")
        if path.read_bytes() == model.read_bytes()
    ]
    if len(records) != 1:
        raise ValueError("expected one final archive record matching the delivered MPD")
    record = records[0]
    output.mkdir(parents=True, exist_ok=True)
    asset_names = [
        "task.json",
        "cameras.json",
        "palette.csv",
        "goal-angle.png",
        "goal-face.png",
        "goal-end.png",
        "goal-top.png",
        "goal-reverse.png",
    ]
    for asset in asset_names:
        shutil.copyfile(public / asset, output / asset)
    shutil.copyfile(record / "build.py", output / "baseline-build.py")
    shutil.copyfile(model, output / "baseline-model.mpd")
    (output / "features.json").write_text(
        json.dumps(FEATURES, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        name: digest(output / name)
        for name in [
            *asset_names,
            "baseline-build.py",
            "baseline-model.mpd",
            "features.json",
        ]
    }
    (output / "replay-manifest.json").write_text(
        json.dumps(
            {"schema_version": 1, "task_id": "R-31039", "sha256": manifest}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    prepare(args.dataset, args.archive, args.output)


if __name__ == "__main__":
    main()
