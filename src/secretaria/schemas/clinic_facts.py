"""Bounded, optional facts about a clinic that the LLM may quote (TASK-025).

Lives in `tenants.clinic_facts` (JSON). Every field is optional and every size is capped
here, on the server, so the prompt block built from it has a known worst case
(`ai/prompts.py::CLINIC_FACTS_BUDGET`). The clinic's ADDRESS is not duplicated here:
it stays in `tenants.address`.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

_Payment = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
_Document = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


def _blank_to_none(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _drop_blank_items(value):
    if isinstance(value, list):
        return [v for v in value if not (isinstance(v, str) and not v.strip())]
    return value


class FaqItem(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]


class ClinicFacts(BaseModel):
    parking: str | None = Field(default=None, max_length=300)
    how_to_arrive: str | None = Field(default=None, max_length=400)
    payment_methods: list[_Payment] = Field(default_factory=list, max_length=10)
    cancellation_policy: str | None = Field(default=None, max_length=500)
    documents_to_bring: list[_Document] = Field(default_factory=list, max_length=10)
    accessibility: str | None = Field(default=None, max_length=300)
    faq: list[FaqItem] = Field(default_factory=list, max_length=15)
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator(
        "parking", "how_to_arrive", "cancellation_policy", "accessibility", "notes", mode="before"
    )
    @classmethod
    def _strip_text(cls, value):
        return _blank_to_none(value)

    @field_validator("payment_methods", "documents_to_bring", mode="before")
    @classmethod
    def _clean_lists(cls, value):
        return _drop_blank_items(value)
