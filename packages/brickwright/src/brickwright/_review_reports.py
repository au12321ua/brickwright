"""Validate the reports returned by the host or a separate visual reviewer."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ConditionStatus = Literal[
    "satisfied", "unmet", "insufficient_evidence", "structural_evidence_required"
]
FeatureStatus = Literal["retained", "partial", "missing", "simplified"]
Verdict = Literal["passed", "needs_changes"]


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(ReviewModel):
    view: Nonempty
    bbox: tuple[int, int, int, int]
    observation: Nonempty


class ConditionResult(ReviewModel):
    id: Nonempty
    status: ConditionStatus
    explanation: Nonempty
    evidence: list[Evidence]
    repair: Nonempty | None = None

    @model_validator(mode="after")
    def require_support(self) -> ConditionResult:
        if self.status in ("satisfied", "unmet") and not self.evidence:
            raise ValueError("a visible condition conclusion requires image evidence")
        if self.status != "satisfied" and self.repair is None:
            raise ValueError(
                "an unresolved condition requires a repair or verification step"
            )
        return self


class FeatureResult(ReviewModel):
    id: Nonempty
    status: FeatureStatus
    evidence: list[Evidence] = Field(min_length=1)
    repair: Nonempty | None = None

    @model_validator(mode="after")
    def require_repair(self) -> FeatureResult:
        if self.status in ("partial", "missing") and self.repair is None:
            raise ValueError("an unresolved feature requires a repair")
        return self


class Finding(ReviewModel):
    description: Nonempty
    check_id: Nonempty | None = None
    required: bool
    evidence: list[Evidence] = Field(min_length=1)
    repair: Nonempty


class Judgment(ReviewModel):
    conditions: list[ConditionResult]
    features: list[FeatureResult]
    findings: list[Finding]
    verdict: Verdict


class Reviewer(ReviewModel):
    session_id: Nonempty
    parent_session_id: Nonempty
    model: Nonempty
    effort: Nonempty
    dispatch: Literal["self", "subagent", "codex_subagent", "independent_model"]
    image_reads: list[Nonempty] = Field(min_length=1)

    @model_validator(mode="after")
    def check_session(self) -> Reviewer:
        if self.dispatch == "self":
            if self.session_id != self.parent_session_id:
                raise ValueError("a self-review must use the builder session")
        elif self.session_id == self.parent_session_id:
            raise ValueError("the reviewer must run in a separate session")
        return self


class Report(Judgment):
    schema_version: Literal[1]
    round_id: str
    input_sha256: str
    reviewer: Reviewer


class FailedReport(ReviewModel):
    schema_version: Literal[1]
    round_id: str
    input_sha256: str
    failure_reason: Nonempty


FIXED_GUIDANCE = """Open the target and candidate for every view, then use the metrics and overlays
to locate differences. The overlays mark target-only pixels in orange,
candidate-only pixels in blue, and RGB differences in magenta. Scores help
locate differences. Decide from the task and the target images."""

MATCHED_GUIDANCE = """Open the target and candidate. The viewpoint was matched approximately.
Judge from the images and the task. Give all evidence boxes in the reference image's coordinate system."""

REVIEW_GUIDANCE = """Use this round's manifest and frozen files to compare the candidate with the target.
Pixel coordinates start at the top left. Each box is [left, top, right, bottom]
with right and bottom excluded.

Review the original conditions first, then every feature. Use
structural_evidence_required for conditions you cannot verify from the images
because they need internal structural evidence, and describe the verification
needed. BrickWright verifies those conditions with the record's frozen checks
and lists any unresolved condition as a remaining item that prevents the round
from passing.
Apply simplified only when the corresponding feature explicitly records a
simplification and the candidate follows it.
Report additional visible discrepancies in findings, including differences in
colour regions, markings and proportions. Give the original check ID when
relevant, otherwise cite the target view and region only. Flag a finding as
required when it must be addressed to satisfy the task or keep an essential
feature, or when it changes a view's outline, colour layout or proportions and
the builder can still repair it with the allowed parts. Leave such a finding
unrequired only when the parts, piece limit or connections prevent a closer
match, and state in the repair why. Include observable evidence and a practical
repair for each open item.
Return passed only if all other original conditions are satisfied, all required
features are retained or have the agreed simplification, and no required
finding remains. Review the supplied images and task files only. Return the
report to the parent for submission."""
