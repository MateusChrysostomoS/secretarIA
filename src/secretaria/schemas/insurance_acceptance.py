"""Schemas for the convênio acceptance matrix (TASK-086 T3, contract in tasks/TASK-086/results/inventario.md §6.2).

The matrix stores only what is NOT accepted (exceptions). Every id travels as text; the API parses
and validates them against the scope (an unknown or malformed id is a 422 with the ids listed).
Request models forbid extra fields: the client never sends tenant/professional/plan columns.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from secretaria.core.catalog_limits import INSURANCE_PLANS_PER_CLINIC, SERVICES_PER_CLINIC

MAX_EXCEPTIONS = SERVICES_PER_CLINIC * INSURANCE_PLANS_PER_CLINIC
MAX_COPY_TARGETS = 50


class MatrixService(BaseModel):
    id: str
    name: str


class MatrixPlan(BaseModel):
    id: str
    name: str
    is_custom: bool


class MatrixCell(BaseModel):
    """One refused (service, plan) cell inside a slice."""

    model_config = ConfigDict(extra="forbid")

    service_id: str = Field(min_length=1, max_length=64)
    plan_id: str = Field(min_length=1, max_length=64)


class MatrixSlice(BaseModel):
    unit_id: str | None
    unit_name: str | None
    offered_service_ids: list[str]
    exceptions: list[MatrixCell]


class InsuranceMatrixRead(BaseModel):
    mode: Literal["shared", "independent"]
    scope: Literal["clinic", "professional"]
    professional_id: str | None
    services: list[MatrixService]
    plans: list[MatrixPlan]
    slices: list[MatrixSlice]
    services_without_id: list[str]


class InsuranceMatrixSliceUpdate(BaseModel):
    """PUT body: REPLACES every exception of the slice (empty = the slice accepts everything)."""

    model_config = ConfigDict(extra="forbid")

    exceptions: list[MatrixCell] = Field(max_length=MAX_EXCEPTIONS)


class InsuranceMatrixCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_unit_id: str = Field(min_length=1, max_length=64)
    to_unit_ids: list[str] = Field(max_length=MAX_COPY_TARGETS)


class PlanOrderUpdate(BaseModel):
    """PUT .../professionals/{id}/insurance-acceptance/plan-order (TASK-090 C3).

    `plan_ids` are the matrix's `plans[].id` (this doctor's own plan rows), left to right. It may
    be a subset: the clinic's place of every plan not listed is kept.
    """

    model_config = ConfigDict(extra="forbid")

    plan_ids: list[UUID] = Field(max_length=INSURANCE_PLANS_PER_CLINIC)


class MatrixUnitCount(BaseModel):
    unit_id: str | None
    exception_count: int


class MatrixCount(BaseModel):
    exception_count: int
    by_unit: list[MatrixUnitCount]


class ProfessionalMatrixCount(MatrixCount):
    professional_id: str


class InsuranceMatrixSummary(BaseModel):
    mode: Literal["shared", "independent"]
    clinic: MatrixCount | None
    professionals: list[ProfessionalMatrixCount]
