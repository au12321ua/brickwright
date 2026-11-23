"""Contracts and immutable assemblies for independent module builds."""

from .contracts import ModuleError, validate_contracts
from .merge import (
    FrozenMerge,
    load_merge,
    merge_context,
    merge_evidence,
    merge_state,
    module_owners,
    module_status,
    place_module,
    prepare_merge,
    verify_merge,
)
from .motion import capture_module_motion
from .records import (
    check_module,
    child_ref,
    finish_module_session,
    init_module,
    module_instructions,
)

__all__ = [
    "FrozenMerge",
    "ModuleError",
    "capture_module_motion",
    "check_module",
    "child_ref",
    "finish_module_session",
    "init_module",
    "load_merge",
    "merge_context",
    "merge_evidence",
    "merge_state",
    "module_instructions",
    "module_owners",
    "module_status",
    "place_module",
    "prepare_merge",
    "validate_contracts",
    "verify_merge",
]
