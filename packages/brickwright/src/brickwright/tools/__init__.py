"""Workflow operations, loaded on first access so importing one skips the rest."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .diagnostics import Diagnostic
    from .exploration import SnippetResult, explore
    from .finalization import (
        DeliveryVerification,
        FinalizationReport,
        finalize,
        verify_delivery,
    )
    from .rendering import (
        RenderReport,
        angle_to_view_dir,
        render,
        render_angle,
        render_available,
        render_views,
    )
    from .testing import CheckResult, TestContext
    from .validation import ValidationReport, validate

_SUBMODULES = {
    "diagnostics": ("Diagnostic",),
    "exploration": ("SnippetResult", "explore"),
    "finalization": (
        "DeliveryVerification",
        "FinalizationReport",
        "finalize",
        "verify_delivery",
    ),
    "rendering": (
        "RenderReport",
        "angle_to_view_dir",
        "render",
        "render_angle",
        "render_available",
        "render_views",
    ),
    "testing": ("CheckResult", "TestContext"),
    "validation": ("ValidationReport", "validate"),
}
_SOURCE = {name: module for module, names in _SUBMODULES.items() for name in names}

__all__ = [
    "CheckResult",
    "DeliveryVerification",
    "Diagnostic",
    "FinalizationReport",
    "RenderReport",
    "SnippetResult",
    "TestContext",
    "ValidationReport",
    "angle_to_view_dir",
    "explore",
    "finalize",
    "render",
    "render_angle",
    "render_available",
    "render_views",
    "validate",
    "verify_delivery",
]


def __getattr__(name: str) -> Any:
    if name not in _SOURCE:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{_SOURCE[name]}", __name__), name)
    globals()[name] = value
    return value
