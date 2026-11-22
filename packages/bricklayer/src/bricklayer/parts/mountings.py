"""Mountings verified against particular part, endpoint, and pose combinations."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

from ..geometry import Ball, Cylindrical, Mounting, Pose, Revolute
from ..geometry.connections import mate_pose, mating_rule, mounting_pose
from ..ldraw.partdata import record

if TYPE_CHECKING:
    from ..modeling.articulation import MotionRange


@dataclass(frozen=True, slots=True)
class VerifiedMounting:
    """Recorded fit for two parts and specific endpoints.

    The first part is the anchor. Endpoint indices refer to each part's
    effective connector order. The mounting parameters are relative to seated
    connector frames. The pose places the second part in the first part's
    element frame. Pose translations are in studs on x/y and bricks on z.
    Motion, when present, describes the sample range relative to the pose.
    The samples establish clearance at individual configurations.
    """

    part_ids: tuple[str, str]
    endpoint_indices: tuple[int, int]
    mounting: str
    parameters: Mounting
    pose: Pose
    verification_scope: str
    sources: tuple[str, ...]
    motion: MotionRange | None = None
    samples_per_axis: int | None = None


def verified_mountings(part_id: str | None = None) -> tuple[VerifiedMounting, ...]:
    """Return the recorded fits involving `part_id`, or all fits if omitted."""
    all_mountings = _all_mountings()
    if part_id is None:
        return all_mountings
    return tuple(row for row in all_mountings if part_id.casefold() in row.part_ids)


def _mounting(
    part_ids: tuple[str, str],
    endpoint_indices: tuple[int, int],
    name: str,
    parameters: Mounting,
    scope: str,
    sources: tuple[str, ...] = (),
    *,
    motion: MotionRange | None = None,
    samples_per_axis: int | None = None,
) -> VerifiedMounting:
    anchor, moving = (
        record(part_id).connectors[index]
        for part_id, index in zip(part_ids, endpoint_indices)
    )
    rule = mating_rule(anchor.type, moving.type)
    if rule is None:
        raise ValueError(f"Mounting {name} no longer has compatible endpoints")
    pose = mounting_pose(parameters, anchor, rule.joint).compose(
        mate_pose(moving, anchor)
    )
    return VerifiedMounting(
        part_ids,
        endpoint_indices,
        name,
        parameters,
        pose,
        scope,
        (*tuple(f"parts/{part_id}.dat" for part_id in part_ids), *sources),
        motion,
        samples_per_axis,
    )


@cache
def _all_mountings() -> tuple[VerifiedMounting, ...]:
    from ..modeling.articulation import MotionRange

    return (
        _mounting(
            ("2650", "2651"),
            (1, 0),
            "retracted",
            Mounting(offset=0.925),
            "Official retracted pose, axial engagement and collider clearance. "
            "Verified at this position only.",
            ("parts/2651c01-f1.dat",),
        ),
        _mounting(
            ("2650", "2651"),
            (1, 0),
            "extended",
            Mounting(offset=-0.925),
            "Official extended pose, axial engagement and collider clearance. "
            "Verified at this position only.",
            ("parts/2651c01-f2.dat",),
        ),
        *(
            _mounting(
                ("30553", "53923"),
                (1, 1),
                f"detent_{angle:g}_degrees",
                Mounting(angle=angle),
                "Centered, indexed tooth position. Verified endpoint engagement "
                "and collider clearance at this angle. Rotation between detents and "
                "snap-in assembly are outside the verification scope.",
                ("p/clh5.dat", "p/clh5s.dat", "p/clh15.dat"),
            )
            for angle in (90, 112.5, 135, 157.5, 180, 202.5, 225, 247.5, 270)
        ),
        _mounting(
            ("4275b", "4276b"),
            (5, 5),
            "flat",
            Mounting(angle=180),
            "Coincident finger axes. Seven collision-free samples from flat to "
            "a 90° fold (0° to -90° relative to flat).",
            motion=MotionRange(Revolute(-90), Revolute(0)),
            samples_per_axis=7,
        ),
        _mounting(
            ("3700", "2780"),
            (8, 0),
            "one_pin_end_seated",
            Mounting(),
            "One end centered in the round hole. Verified axial engagement and "
            "collider clearance at the seated depth.",
        ),
        _mounting(
            ("7229", "3705"),
            (4, 0),
            "cross_axle_centered",
            Mounting(),
            "Both cross sections aligned. Verified axial engagement and collider "
            "clearance at the centered position.",
        ),
        _mounting(
            ("14418", "22890"),
            (8, 8),
            "ball_centered",
            Mounting(angle=180),
            "Ball centered in the socket. All 27 samples at swing coordinates "
            "-15°, 0°, 15° on both axes and twist -20°, 0°, 20° cleared. "
            "Four of 27 samples collided with swing ±20° and twist ±30°.",
            motion=MotionRange(Ball(-15, -15, -20), Ball(15, 15, 20)),
            samples_per_axis=3,
        ),
        _mounting(
            ("30237", "30031"),
            (3, 0),
            "side_bar_in_clip",
            Mounting(angle=180),
            "First angled handlebar seated in the vertical clip. Seven collision-free "
            "twist samples from -30° to 30° about this pose at fixed insertion. "
            "Eleven of 25 samples collided over a full turn.",
            motion=MotionRange(Cylindrical(angle=-30), Cylindrical(angle=30)),
            samples_per_axis=7,
        ),
        _mounting(
            ("11211", "3024"),
            (5, 0),
            "front_side_stud",
            Mounting(),
            "Plate seated on the front-facing stud at local x=-0.5 studs. "
            "Verified facing, engagement and collider clearance.",
        ),
    )
