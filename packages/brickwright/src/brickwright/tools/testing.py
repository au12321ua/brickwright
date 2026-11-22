"""The check harness a record's ``run_tests()`` uses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from .diagnostics import Diagnostic
from .placement_evidence import PlacementEvidence

if TYPE_CHECKING:
    from bricklayer import Element, LegoBuild, Placement

# Cap how many collision/floating items we echo back, to keep details compact.
_MAX_SAMPLE = 20


class CheckResult(BaseModel):
    """The outcome of a single check: did it pass, and why not if it failed."""

    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    detail: str | None = None
    diagnostics: list[Diagnostic] = Field(
        default_factory=list, exclude_if=lambda diagnostics: not diagnostics
    )


class TestContext:
    """Accumulates :class:`CheckResult` outcomes for a single build."""

    __test__ = False  # prevent pytest from collecting this class

    def __init__(self, build: LegoBuild) -> None:
        self.build = build
        self._results: list[CheckResult] = []

    def check(
        self,
        name: str,
        ok: bool,
        detail: str | None = None,
        *,
        diagnostics: list[Diagnostic] | None = None,
    ) -> CheckResult:
        """Record a check; ``detail`` explains a failure (dropped when it passes)."""
        passed = bool(ok)
        result = CheckResult(
            name=name,
            passed=passed,
            detail=None if passed else detail,
            diagnostics=[] if passed else diagnostics or [],
        )
        self._results.append(result)
        return result

    def check_no_floating(self) -> CheckResult:
        """Pass when nothing floats -- every part is supported down to the ground."""
        floaters = self.build.floating_placements
        if not floaters:
            return self.check("no_floating", True)
        sampled = floaters[:_MAX_SAMPLE]
        sample = "; ".join(_describe_placement(self.build, p) for p in sampled)
        detail = f"{len(floaters)} floating: {sample}"
        locations = PlacementEvidence(self.build)
        return self.check(
            "no_floating",
            False,
            detail,
            diagnostics=[
                Diagnostic(
                    code="validation.floating",
                    stage="validate",
                    message=f"{len(floaters)} placement(s) lack support to ground.",
                    evidence={
                        "count": len(floaters),
                        "placements": [locations.placement(part) for part in sampled],
                    },
                    next_action="Add a supported connection or bring the reported placements into contact with a support.",
                )
            ],
        )

    def check_no_collisions(self) -> CheckResult:
        """Pass when no two parts collide."""
        pairs = self.build.colliding_placements
        if not pairs:
            return self.check("no_collisions", True)
        sampled = pairs[:_MAX_SAMPLE]
        sample = "; ".join(
            f"{_describe_placement(self.build, a)}  overlaps  "
            f"{_describe_placement(self.build, b)}"
            for a, b in sampled
        )
        detail = f"{len(pairs)} collisions: {sample}"
        locations = PlacementEvidence(self.build)
        return self.check(
            "no_collisions",
            False,
            detail,
            diagnostics=[
                Diagnostic(
                    code="validation.collision",
                    stage="validate",
                    message=f"{len(pairs)} pair(s) of placements collide.",
                    evidence={
                        "count": len(pairs),
                        "pairs": [
                            [locations.placement(a), locations.placement(b)]
                            for a, b in sampled
                        ],
                    },
                    next_action="Inspect the reported pairs and adjust their relative position or mounting.",
                )
            ],
        )

    def check_connected(self, expected_components: int = 1) -> CheckResult:
        """Pass when the parts form exactly ``expected_components`` components.

        Compatible mated connectors join parts; merely touching faces do not.
        The default
        expects a single component; a legitimately multipart build -- say a
        scene with two separate buildings -- passes its expected count. This
        says nothing about the ground: a hovering but internally joined stack
        is connected.
        """
        components = self.build.connected_components
        n_components = len(components)
        if n_components == expected_components:
            return self.check("connected", True)
        detail = (
            f"Expected {expected_components} connected component(s), "
            f"found {n_components}"
        )
        locations = PlacementEvidence(self.build)
        return self.check(
            "connected",
            False,
            detail,
            diagnostics=[
                Diagnostic(
                    code="validation.connectivity",
                    stage="validate",
                    message=detail,
                    evidence={
                        "expected": expected_components,
                        "actual": n_components,
                        "components": [
                            {
                                "index": i,
                                "count": len(component),
                                "placements": [
                                    locations.placement(part)
                                    for part in component[:_MAX_SAMPLE]
                                ],
                            }
                            for i, component in enumerate(components[:_MAX_SAMPLE])
                        ],
                    },
                    next_action=(
                        "Join the reported components using mating connectors."
                        if n_components > expected_components
                        else "Inspect which planned components are joined and adjust their connections."
                    ),
                )
            ],
        )

    def check_motion(
        self, *, samples: int = 5, max_samples: int = 10000
    ) -> CheckResult:
        """Optionally check annotated motion for collisions, floating, and disengagement.

        Samples every combination of the declared ranges, including rest and limits.
        Unannotated connections remain rigid, so the check can flag motions that
        would be possible with additional flexibility. A pass covers only the
        samples. Invalid ranges, motion loops, or an exceeded sample budget raise.
        """
        report = self.build.check_motion(samples=samples, max_samples=max_samples)
        if report.passed:
            return self.check("motion", True)
        problems = []
        for failure in report.collisions[:_MAX_SAMPLE]:
            a, b = failure.placements
            problems.append(
                f"collision: {_describe_placement(self.build, a)} overlaps "
                f"{_describe_placement(self.build, b)} at "
                f"{[coordinates for _, coordinates in failure.positions]}"
            )
        for failure in report.floating[:_MAX_SAMPLE]:
            parts = "; ".join(
                _describe_placement(self.build, p)
                for p in failure.placements[:_MAX_SAMPLE]
            )
            problems.append(
                f"floating: {parts} at "
                f"{[coordinates for _, coordinates in failure.positions]}"
            )
        if report.disconnections:
            problems.append(f"{len(report.disconnections)} connector disengagements")
        detail = f"{report.samples_checked} samples checked; " + "; ".join(problems)
        locations = PlacementEvidence(self.build)
        diagnostics = []
        if report.collisions:
            diagnostics.append(
                Diagnostic(
                    code="motion.collision",
                    stage="check_motion",
                    message=f"{len(report.collisions)} sampled collision(s).",
                    evidence={
                        "count": len(report.collisions),
                        "samples_checked": report.samples_checked,
                        "samples": [
                            {
                                "placements": [
                                    locations.placement(part)
                                    for part in failure.placements
                                ],
                                "positions": locations.positions(failure.positions),
                            }
                            for failure in report.collisions[:_MAX_SAMPLE]
                        ],
                    },
                    next_action="Inspect the joint mounting and clearance between the reported placements at the sampled positions.",
                )
            )
        if report.floating:
            diagnostics.append(
                Diagnostic(
                    code="motion.floating",
                    stage="check_motion",
                    message=f"Placements lose support at {len(report.floating)} sampled position(s).",
                    evidence={
                        "count": len(report.floating),
                        "samples_checked": report.samples_checked,
                        "samples": [
                            {
                                "count": len(failure.placements),
                                "placements": [
                                    locations.placement(part)
                                    for part in failure.placements[:_MAX_SAMPLE]
                                ],
                                "positions": locations.positions(failure.positions),
                            }
                            for failure in report.floating[:_MAX_SAMPLE]
                        ],
                    },
                    next_action="Provide support that follows the reported assemblies through the sampled motion.",
                )
            )
        if report.disconnections:
            diagnostics.append(
                Diagnostic(
                    code="motion.disconnected",
                    stage="check_motion",
                    message=f"{len(report.disconnections)} sampled connector disengagement(s).",
                    evidence={
                        "count": len(report.disconnections),
                        "samples_checked": report.samples_checked,
                        "samples": [
                            {
                                "joint": locations.joint(failure.joint),
                                "positions": locations.positions(failure.positions),
                            }
                            for failure in report.disconnections[:_MAX_SAMPLE]
                        ],
                    },
                    next_action="Inspect the reported endpoints and adjust insertion or travel to keep them engaged.",
                )
            )
        return self.check("motion", False, detail, diagnostics=diagnostics)

    def report(self) -> list[CheckResult]:
        """The check results in the order they were run."""
        return list(self._results)


def _describe_placement(build: LegoBuild, placement: Placement[Element]) -> str:
    label = build.name_of(placement) or placement.content.spec.name
    return f"{label} @ {placement.pose.translation}"
