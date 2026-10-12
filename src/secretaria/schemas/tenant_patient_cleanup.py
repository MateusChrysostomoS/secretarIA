"""Counts-only contract for clinic-scoped patient cleanup."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool = False
    clinic_name: str = Field(min_length=1, max_length=255)


class CleanupResult(BaseModel):
    status: Literal["ready", "completed", "not_provisioned", "blocked", "failed"]
    counts: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
