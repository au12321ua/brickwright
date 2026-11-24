"""Settings for BrickWright records and workflow operations."""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# Where records live by default, and the env var that overrides it.
DEFAULT_RECORDS_DIR = "records"
RECORDS_DIR_ENV = "BRICKWRIGHT_RECORDS_DIR"


def default_flow() -> bool:
    return os.getenv("BRICKWRIGHT_FLOW", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def default_records_dir() -> Path:
    return Path(os.environ.get(RECORDS_DIR_ENV, DEFAULT_RECORDS_DIR))


class Config(BaseModel):
    """Configure record creation and execution of workflow operations."""

    model_config = ConfigDict(frozen=True)

    records_dir: Path = Field(default_factory=default_records_dir)
    flow: bool = Field(default_factory=default_flow)
    flow_review_rounds: int = Field(default=2, ge=0)
    max_validations: int = Field(default=50, ge=0)  # 0 disables the validation cap
    build_timeout: float = 30.0
    render_timeout: float = 60.0
