"""Structured facts attached to recoverable BrickLayer operation errors."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import JsonValue

    from .geometry.connections import Connector


class DiagnosticError(ValueError):
    """A value error with a stable reason and JSON-compatible evidence."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "execution.exception",
        evidence: dict[str, JsonValue] | None = None,
        next_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.evidence = {} if evidence is None else evidence
        self.next_action = next_action


def connector_evidence(connector: Connector) -> dict[str, JsonValue]:
    """An endpoint's type, position, orientation, and reach in its input frame."""
    return {
        "type": connector.type.name,
        "position": [float(value) for value in connector.position],
        "axis": [float(value) for value in connector.axis],
        "side": [float(value) for value in connector.side],
        "length": float(connector.length),
    }
