"""TASK-085 T4c: the AI and the texts follow D3 (pure readers, no DB).

`RuntimeAppointmentType` is what the prompt and `get_service_info` read; the scoped-help block
and the "Próxima consulta" line read the professional's entries and the roster snapshot
(`service_unit_terms`, `units`, `service_units` - services/unit_booking.py). Characterization
tests pin today's text for a clinic without per-unit values; the others prove that a hidden
price (it differs between the units) is never rendered and that a unit's own value is.
"""

import os

os.environ.setdefault("APP_ENV", "test")

from datetime import UTC, datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

from secretaria.ai.prompts import _format_appointment_types, secretary_system_prompt  # noqa: E402
from secretaria.ai.prompts_v2 import _format_clinic_context_v2  # noqa: E402
from secretaria.ai.scoped_help import _service_help_prompt, _service_options_block  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    RuntimeAppointmentType,
    TenantRuntimeConfig,
)

NOW = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)


def _type(name="Consulta", *, price="R$ 200,00", duration=30, **kw) -> RuntimeAppointmentType:
    return RuntimeAppointmentType(
        name=name,
        description=kw.get("description"),
        duration_min=duration,
        price=price,
        long_description=kw.get("long_description"),
        requirements=kw.get("requirements", []),
    )


def _config(types) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=UUID(int=7),
        clinic_name="Clínica Teste",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=types,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        google_calendar_id="primary",
        google_refresh_token=None,
    )


_TYPES = [
    _type("Consulta", requirements=["Trazer exames"], description="Avaliação"),
    _type("Retorno", price=None, duration=20),
    _type("Exame", price="", duration=15, long_description="Detalhes"),
]
_TODAY_SECTION = (
    "- Consulta (30 min) - R$ 200,00 — Avaliação (há orientações: use get_service_info)\n"
    "- Retorno (20 min)\n"
    "- Exame (15 min) (há orientações: use get_service_info)"
)


# ======================================================================== B3 prompt text


def test_char_b3_services_section_is_todays():
    assert _format_appointment_types(_TYPES, 30) == _TODAY_SECTION
    assert _format_appointment_types([], 45) == "- Consulta (45 minutos)"
    prompt = secretary_system_prompt(_config(_TYPES), now=NOW)
    assert _TODAY_SECTION in prompt
    v2 = _format_clinic_context_v2(_config(_TYPES), frozenset({"get_service_info"}))
    assert v2.endswith(
        "- Serviços (nome exato, duração e preço quando a clínica cadastrou):\n" + _TODAY_SECTION
    )


# =================================================================== B5 scoped-help block


_SERVICES = [
    {"name": "Primeira Consulta", "price": "R$ 250", "description": "Avaliação completa."},
    {"name": "Retorno"},
]


def test_char_b5_options_block_has_no_money():
    # TASK-088 R38: name and description only - the price the entry carries is not shown.
    assert _service_options_block(_SERVICES) == (
        "- Primeira Consulta — Avaliação completa.\n- Retorno"
    )
    prompt = _service_help_prompt(_SERVICES, False)
    assert "- Primeira Consulta — Avaliação completa." in prompt
    assert "R$" not in prompt


# ============================================================== shared roster snapshot shape


def _unit(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), name=name, address=None, business_hours={"monday": [{"start": "08:00"}]}
    )


def _doctor(units, terms=None, entries=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        name="Dra. Ana",
        units=units,
        service_units={},
        service_unit_terms=terms or {},
        appointment_types=entries,
    )


def _appt(professional_id=None, unit_id=None, service="Consulta") -> dict:
    return {
        "id": str(uuid4()),
        "appointment_type": service,
        "start_at": datetime(2026, 12, 7, 14, 0, tzinfo=UTC),
        "end_at": datetime(2026, 12, 7, 14, 30, tzinfo=UTC),
        "professional_id": str(professional_id) if professional_id else None,
        **({"unit_id": str(unit_id)} if unit_id else {}),
    }


# =========================================================================================
# With per-unit values (each fails before TASK-085 T4c)
# =========================================================================================

SID = uuid4()
ENTRY = {"name": "Consulta", "service_id": str(SID), "price": "R$ 200,00", "duration_min": 30}
HIDDEN_TEXT = (
    "valor depende da unidade escolhida (só informe depois que o paciente escolher a unidade; "
    "nunca uma faixa)"
)


def _hidden(**kw) -> RuntimeAppointmentType:
    return RuntimeAppointmentType(
        name="Consulta", description=None, duration_min=30, price=None, price_hidden=True, **kw
    )


def test_b3_hidden_price_renders_a_plain_sentence_never_a_number():
    section = _format_appointment_types([_hidden(), *_TYPES[1:]], 30)
    first = section.splitlines()[0]
    assert first == f"- Consulta (30 min) - {HIDDEN_TEXT}"
    assert section.splitlines()[1:] == _TODAY_SECTION.splitlines()[1:]
    v2 = _format_clinic_context_v2(_config([_hidden()]), frozenset())
    assert v2.endswith(f"- Consulta (30 min) - {HIDDEN_TEXT}")


def test_b4_the_flag_is_an_allowlisted_output_key():
    from secretaria.ai.tool_output import undeclared_keys

    assert undeclared_keys("get_service_info", {"preco_depende_da_unidade": True}) == []


def test_b5_options_block_never_has_money_nor_the_price_depends_sentence():
    # TASK-088 R38 ("valor só no fim"): whatever the entries carry, the block is money-free and
    # never says "o valor depende da unidade" (that told the model to say the price later).
    services = [ENTRY, {"name": "Retorno", "price": "R$ 90"}]
    block = _service_options_block(services)
    assert block == "- Consulta\n- Retorno"
    assert "R$" not in block and "valor depende" not in block


def test_a13_cannot_tell_the_service_means_no_price_never_a_wrong_one():
    from secretaria.services.service_terms import UnitTerms
    from secretaria.workers.shared.llm_context import _appointment_context_text

    centro = _unit("Centro")
    terms = {SID: {centro.id: UnitTerms("R$ 250,00", None)}}
    types = [_type("Consulta")]  # the runtime list's (today's) price: R$ 200,00
    own = _doctor([centro], terms, entries=[ENTRY])
    inherits = _doctor([centro], terms, entries=None)
    renamed = _doctor([centro], terms, entries=[{**ENTRY, "name": "Outra"}])
    plain = _doctor([centro], {}, entries=[ENTRY])

    def line(doctor, *, roster=True):
        text = _appointment_context_text(
            [_appt(doctor.id, centro.id)],
            "America/Sao_Paulo",
            {},
            types,
            professionals=[doctor] if roster else [],
        )
        return text.splitlines()[0]

    assert line(own).endswith("— Consulta — R$ 250,00")
    assert line(inherits).endswith("— Consulta")  # R9: inherited list with rows: unknown
    assert line(renamed).endswith("— Consulta")  # the owner's entry is gone: unknown
    assert line(plain).endswith("— Consulta — R$ 200,00")  # no row for him: today's
    assert line(own, roster=False).endswith("— Consulta — R$ 200,00")  # off the roster: today's
