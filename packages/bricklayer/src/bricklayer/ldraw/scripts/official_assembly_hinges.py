"""Official rigid hinge references for P-007."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...geometry import Pose, rotation_about
from ..conversion import LDrawVec
from .official_assemblies import Negative, Sample
from .official_assembly_inventory import read_assembly
from .official_snapshot import Snapshot

HINGE_RULE_VERSION = "hinge-1"


@dataclass(frozen=True)
class _Hinge:
    source: str
    host: str
    child: str
    subtype: int
    evidence: str
    vertical: bool = False
    reverse_polarity: bool = False


# The parts are identified by their geometry/roles in the indicated source,
# independently of the SDK candidate list. Keep variants and open/closed
# angles separate. The mixed assemblies also contain unrelated interfaces.
_REFERENCES = (
    _Hinge(
        "18904c01", "18904", "18905", 32, "The crocodile's jaw hinge seats in the body."
    ),
    _Hinge(
        "3135c01",
        "3135",
        "3136",
        59,
        "The crane hook seats on the crane arm's end pivot.",
    ),
    _Hinge(
        "3149ec01",
        "314e",
        "3149e",
        19,
        "The two hinge-plate leaves have coaxial knuckles around the retained pivot 313.",
        reverse_polarity=True,
    ),
    _Hinge(
        "3324ec01",
        "314e",
        "3324e",
        19,
        "The longer base engages the same hinge-plate top at the retained pivot 313.",
        reverse_polarity=True,
    ),
    _Hinge(
        "3937c01",
        "3937",
        "3938",
        12,
        "The hinge brick top clips onto the base's integral pivot.",
    ),
    _Hinge(
        "52258p01c01",
        "52258",
        "51344p01",
        28,
        "The dragon head seats on the assembled body's neck pivot.",
        reverse_polarity=True,
    ),
    _Hinge(
        "6026c01",
        "6026",
        "6027",
        20,
        "The crocodile jaw engages the body's front pivot at the recorded tilt.",
    ),
    _Hinge(
        "73983",
        "2429",
        "2430",
        39,
        "The two hinge-plate leaves share the vertical pivot.",
        vertical=True,
    ),
    _Hinge(
        "75174c01",
        "75174",
        "6027",
        20,
        "The dragon head pivots on the body at the recorded tilt.",
    ),
    _Hinge(
        "75174p01c01",
        "75174p01",
        "43900",
        20,
        "The patterned dragon variant seats its head on the same front pivot.",
    ),
    _Hinge(
        "76409c01",
        "76409",
        "30459",
        28,
        "The dinosaur head seats on the neck pivot at the recorded tilt.",
        reverse_polarity=True,
    ),
    _Hinge(
        "76442c01",
        "30461c02",
        "30460c01",
        20,
        "The assembled head hinges directly on the assembled body's upper pivot.",
    ),
)


def hinge_samples(root: Path, snapshot: Snapshot) -> tuple[Sample, ...]:
    """Read rigid hinge poses as given, retaining their angle in all comparisons."""
    samples: list[Sample] = []
    for rule in _REFERENCES:
        assembly = read_assembly(root, rule.source, snapshot)
        (host,) = (item for item in assembly.direct if item.part_id == rule.host)
        (child,) = (item for item in assembly.direct if item.part_id == rule.child)
        roles = ("in", "on") if rule.reverse_polarity else ("on", "in")
        first, second = (f"hinge_{rule.subtype}_{role}" for role in roles)

        # Slide the leaf along the hinge axis past its seating face while
        # preserving the recorded tilt. The other perturbation rotates across
        # the pivot axis, not along the hinge's permitted degree of freedom.
        displacement = LDrawVec(0, 5, 0) if rule.vertical else LDrawVec(5, 0, 0)
        offset = host.pose.apply(displacement.to_position()) - host.pose.translation
        shifted = Pose(child.pose.rotation, child.pose.translation + offset)
        transverse = (
            host.pose.rotation @ LDrawVec(0, 0, 20).to_position().to_stud_units()
        )
        tilted = Pose(
            rotation_about(transverse, 10) @ child.pose.rotation,
            child.pose.translation,
        )
        samples.append(
            Sample(
                f"{rule.source}/hinge",
                host,
                child,
                frozenset({(first, second)}),
                shifted,
                "Move the seated leaf 5 LDU along the hinge axis, out of its axial seat.",
                "hinge",
                additional_negatives=(
                    Negative(
                        "off_axis_tilt",
                        tilted,
                        "Tilt the moving leaf 10 degrees about a transverse axis so the pivot axes diverge.",
                    ),
                ),
                positive_evidence=rule.evidence,
            )
        )
    return tuple(samples)
