"""The booking details a patient reads before confirming an AI-landed booking (TASK-030 P3).

When the AI's draft carries everything - who it is for, the convênio, the doctor, the
service, the day and the time - the patient skips the steps that would have shown them the
doctor, the service card and its price. Spec §4.4.2: they still receive everything those
steps would have shown, as ONE text message of its own right before the confirmation card:
the doctor (and specialty), the service with its price and description, what to bring / how
to prepare (the service's `requirements`), the convênio, who the booking is for, and the
clinic's address when the clinic has one stored (`Tenant.address`, read by
workers/shared/draft_resolution.py::_load_draft_context).

Pure: plain values in, one string out - no flow_router import (flow_router imports
`price_text` from here), no database, no logging. The text names a third party and the
patient's convênio, so it is sent and never logged.

Size: a text message caps at MAX_TEXT_MESSAGE_CHARS (core/whatsapp_limits.py), and a clinic
may write a 2000-character description and twenty 300-character requirements. The body is
trimmed in a fixed order until it fits - the long description gives way to the short one,
then to none, then the requirements keep their first REQUIREMENTS_KEEP items plus a "…"
line - and only then is it hard-cut with `truncate_plain` (display-only text, nothing ever
matches against it: skill third-party-text-limits). The lines that identify the booking
(doctor, service, price, convênio, who, address) come first and are never the ones trimmed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from secretaria.core.whatsapp_limits import (
    EMOJI_DOCTOR,
    EMOJI_PERSON,
    EMOJI_SERVICE,
    MAX_TEXT_MESSAGE_CHARS,
    TRUNCATION_MARK,
    truncate_plain,
)
from secretaria.services.attendee import real_attendee_name

DETAILS_HEADER = "Confira os detalhes da sua consulta:"
REQUIREMENTS_HEADER = "O que levar / preparo:"
# Requirements kept when the whole list does not fit (followed by a "…" line).
REQUIREMENTS_KEEP = 3
_PRICE_PREFIX = "R$"


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def price_text(price: Any) -> str | None:
    """The price as the patient reads it ("R$250", "R$ 250"), or None when there is none.

    The ONE spelling the service card (`flow_router._service_detail_text`) and the booking
    details share: a value without the "R$" prefix gets it, one that has it is kept.
    """
    if not price:
        return None
    text = str(price).strip()
    if not text.upper().startswith(_PRICE_PREFIX):
        text = f"{_PRICE_PREFIX}{text}"
    return text


def clinic_address_line(address: Any) -> str | None:
    """One line out of `Tenant.address`, or None when there is no street.

    `Tenant.address` is `{"line", "complement", "neighborhood", "city", "state",
    "postal_code"}`, every key optional (schemas/config.py::TenantAddress). The street
    (`line`) is required: a city alone tells nobody where to go, and a plausible but
    incomplete address is worse than none.
    """
    if not isinstance(address, Mapping):
        return None

    def part(key: str) -> str | None:
        value = address.get(key)
        return _clean(value) if isinstance(value, str) else None

    street = part("line")
    if street is None:
        return None
    city, state = part("city"), part("state")
    city_state = f"{city}/{state}" if city and state else city or state
    postal_code = part("postal_code")
    pieces = (
        street,
        part("complement"),
        part("neighborhood"),
        city_state,
        f"CEP {postal_code}" if postal_code else None,
    )
    return ", ".join(piece for piece in pieces if piece)


def _identity_lines(
    service: Mapping[str, Any],
    professional: Any | None,
    insurance: str | None,
    attendee_name: str | None,
    address: str | None,
) -> list[str]:
    lines: list[str] = []
    doctor = _clean(getattr(professional, "name", None)) if professional is not None else None
    if doctor:
        specialty = _clean(getattr(professional, "specialty", None))
        lines.append(
            f"{EMOJI_DOCTOR} Profissional: {doctor}" + (f" — {specialty}" if specialty else "")
        )
    name = _clean(service.get("name")) or "Consulta"
    price = price_text(service.get("price"))
    lines.append(f"{EMOJI_SERVICE} Serviço: {name}" + (f" — {price}" if price else ""))
    plan = _clean(insurance)
    if plan:
        lines.append(f"Convênio: {plan}")
    if attendee_name is not None:
        # "" (ATTENDEE_SELF) is the patient themself; None was never answered: no line.
        lines.append(f"{EMOJI_PERSON} Para: {real_attendee_name(attendee_name) or 'você'}")
    where = _clean(address)
    if where:
        lines.append(f"Endereço: {where}")
    return lines


def _requirements(service: Mapping[str, Any]) -> list[str]:
    raw = service.get("requirements") or []
    if isinstance(raw, str):
        raw = [raw]
    return [text for text in (_clean(item) for item in raw) if text]


def _assemble(
    identity: list[str], description: str | None, requirements: list[str], *, keep: int | None
) -> str:
    blocks = [DETAILS_HEADER, "\n".join(identity)]
    if description:
        blocks.append(description)
    if requirements:
        shown = requirements if keep is None else requirements[:keep]
        bullets = "\n".join(f"• {item}" for item in shown)
        if keep is not None and len(requirements) > keep:
            bullets += f"\n{TRUNCATION_MARK}"
        blocks.append(f"{REQUIREMENTS_HEADER}\n{bullets}")
    return "\n\n".join(blocks)


def booking_details_text(
    *,
    service: Mapping[str, Any],
    professional: Any | None,
    insurance: str | None,
    attendee_name: str | None,
    address: str | None,
    max_chars: int = MAX_TEXT_MESSAGE_CHARS,
) -> str:
    """The details message: everything the skipped steps would have shown, within `max_chars`.

    `service` is the catalog dict of the booking (name, price, description,
    long_description, requirements); `professional` the booking's doctor (name, specialty)
    or None; `insurance` the recorded convênio; `attendee_name` the pra-quem answer
    (ATTENDEE_SELF, a third party's name, or None = never answered); `address` an already
    formatted line (`clinic_address_line`) or None. Lines with nothing to say are left out.
    """
    identity = _identity_lines(service, professional, insurance, attendee_name, address)
    requirements = _requirements(service)
    long_text = _clean(service.get("long_description"))
    short_text = _clean(service.get("description"))
    descriptions: list[str | None] = [long_text or short_text]
    if long_text and short_text and short_text != long_text:
        descriptions.append(short_text)
    descriptions.append(None)
    for description in descriptions:
        body = _assemble(identity, description, requirements, keep=None)
        if len(body) <= max_chars:
            return body
    body = _assemble(identity, None, requirements, keep=REQUIREMENTS_KEEP)
    if len(body) <= max_chars:
        return body
    return truncate_plain(body, max_chars)
