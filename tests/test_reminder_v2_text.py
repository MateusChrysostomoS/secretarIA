"""services/reminder_text.py — the words of the reminder (TASK-032 R2, spec §4.2)."""

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from secretaria.config import get_settings
from secretaria.core.whatsapp_limits import MAX_BUTTON_LABEL_CHARS, MAX_INTERACTIVE_BODY_CHARS
from secretaria.models import Appointment, Tenant
from secretaria.schemas.webhook import WebhookMessage, decode_action_id, extract_action_button
from secretaria.services import reminder_text as rt
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import seed_world

SENTENCE = (
    "LEMBRE-SE: Sua consulta com Dra. Ana Souza está marcada para o dia "
    "10/10/2026 às 14:30 para Consulta."
)


def _content(**overrides) -> rt.ReminderContent:
    base = dict(
        clinic_name="Clínica Olhar",
        timezone="America/Sao_Paulo",
        start_at=datetime(2026, 10, 10, 17, 30, tzinfo=UTC),
        service_name="Consulta",
        doctor_name="Dra. Ana Souza",
    )
    base.update(overrides)
    return rt.ReminderContent(**base)


def test_sentence_is_the_spec_text_in_the_clinic_timezone():
    assert rt.reminder_sentence(_content()) == SENTENCE


def test_day_rollover_uses_the_clinic_day_not_utc():
    content = _content(start_at=datetime(2026, 10, 11, 1, 30, tzinfo=UTC))
    assert "dia 10/10/2026 às 22:30" in rt.reminder_sentence(content)


def test_another_clinic_timezone_is_honoured():
    content = _content(timezone="America/Manaus")  # UTC-4
    assert "dia 10/10/2026 às 13:30" in rt.reminder_sentence(content)


def test_a_naive_start_is_read_as_utc():
    content = _content(start_at=datetime(2026, 10, 10, 17, 30))  # SQLite read-back
    assert rt.reminder_sentence(content) == SENTENCE


def test_an_unknown_timezone_falls_back_to_sao_paulo():
    assert rt.reminder_sentence(_content(timezone="Mars/Olympus")) == SENTENCE


def test_a_booking_for_someone_else_names_the_attendee():
    sentence = rt.reminder_sentence(_content(attendee_name="João Pedro"))
    assert sentence.startswith("LEMBRE-SE: A consulta de João Pedro com Dra. Ana Souza ")


def test_without_a_professional_the_clinic_team_is_named():
    sentence = rt.reminder_sentence(_content(doctor_name=None))
    assert "Sua consulta com a equipe da Clínica Olhar está marcada" in sentence


def test_body_lists_the_requirements_as_bullets():
    body = rt.build_reminder_body(_content(requirements=("Jejum de 8h", "Trazer exames.")))
    assert body == (f"{SENTENCE}\n\nOrientações para a consulta:\n• Jejum de 8h\n• Trazer exames")


def test_body_without_requirements_is_just_the_sentence():
    assert rt.build_reminder_body(_content()) == SENTENCE


def test_body_fits_the_interactive_limit_by_dropping_requirements_never_the_sentence():
    many = tuple(f"Orientação número {i} " + "x" * 80 for i in range(20))
    body = rt.build_reminder_body(_content(requirements=many))
    assert len(body) <= MAX_INTERACTIVE_BODY_CHARS
    assert body.startswith(SENTENCE)
    assert body.endswith("\n…")


def test_requirements_with_line_breaks_become_one_valid_template_parameter():
    variables = rt.template_variables(
        _content(requirements=("Jejum de 8h\nsem água", "Trazer\texames     anteriores"))
    )
    assert len(variables) == rt.TEMPLATE_PARAM_COUNT == 6
    for value in variables:
        assert value
        assert "\n" not in value and "\t" not in value and "     " not in value
    assert variables[5] == "Orientações: Jejum de 8h sem água; Trazer exames anteriores."


def test_template_variables_are_in_the_meta_order():
    assert rt.template_variables(_content()) == [
        "Sua consulta",
        "Dra. Ana Souza",
        "10/10/2026",
        "14:30",
        "Consulta",
        "Sem orientações especiais.",
    ]


def test_single_line_text_has_no_line_break():
    line = rt.single_line_text(_content(requirements=("a\nb",)))
    assert "\n" not in line
    assert line == f"{SENTENCE} Orientações: a b."


def test_a_long_requirements_parameter_is_capped_and_marked():
    value = rt.flatten_requirements(("y" * 1000,))
    assert len(value) == rt.REQUIREMENTS_PARAM_MAX_CHARS
    assert value.endswith("…")


async def test_content_is_loaded_from_the_booking(db):  # noqa: F811
    world = await seed_world(
        db,
        professional_name="Dra. Ana Souza",
        requirements=["Jejum de 8h\nsem água"],
        attendee_name="João Pedro",
        timezone="America/Manaus",
    )
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        content = await rt.load_reminder_content(session, tenant, appointment)

    assert content.clinic_name == "Clínica Olhar"
    assert content.timezone == "America/Manaus"
    assert content.doctor_name == "Dra. Ana Souza"
    assert content.service_name == "Consulta"
    assert content.attendee_name == "João Pedro"
    assert content.requirements == ("Jejum de 8h\nsem água",)


async def test_a_professional_of_another_clinic_is_never_named(db):  # noqa: F811
    world = await seed_world(db)
    other = await seed_world(db, professional_name="Dr. Outro", phone_number_id="pnid-2")
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.professional_id = other.appointment.professional_id
        await session.commit()
        tenant = await session.get(Tenant, world.tenant.id)
        content = await rt.load_reminder_content(session, tenant, appointment)
    assert content.doctor_name is None


async def test_a_service_missing_from_the_catalog_keeps_its_name_without_requirements(db):  # noqa: F811
    world = await seed_world(db, requirements=["Jejum"])
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.appointment_type = "Retorno"
        await session.commit()
        tenant = await session.get(Tenant, world.tenant.id)
        content = await rt.load_reminder_content(session, tenant, appointment)
    assert content.service_name == "Retorno"
    assert content.requirements == ()


# --------------------------------------------------------------------------
# Button ids (Task 3)
# --------------------------------------------------------------------------


def _interactive(button_id: str) -> WebhookMessage:
    return WebhookMessage.model_validate(
        {
            "id": "wamid.1",
            "from": "5511999999999",
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": button_id, "title": "x"},
            },
        }
    )


def _quick_reply(payload: str) -> WebhookMessage:
    return WebhookMessage.model_validate(
        {
            "id": "wamid.2",
            "from": "5511999999999",
            "type": "button",
            "button": {"payload": payload, "text": "x"},
        }
    )


@pytest.mark.parametrize("action", ["remconfirm", "remcancel", "remother"])
def test_reminder_ids_decode_on_both_whatsapp_carriers(action):
    reminder_id = str(uuid4())
    raw = f"{action}|{reminder_id}"
    assert extract_action_button(_interactive(raw)) == (action, reminder_id)
    assert extract_action_button(_quick_reply(raw)) == (action, reminder_id)
    assert decode_action_id(raw) == (action, reminder_id)


@pytest.mark.parametrize(
    "raw", [None, "", "remconfirm|not-a-uuid", f"remconfirmx|{uuid4()}", f"rem|{uuid4()}"]
)
def test_a_tampered_or_unknown_id_decodes_to_none(raw):
    assert decode_action_id(raw) is None


@pytest.mark.parametrize(
    "action", ["apptconfirm", "apptresched", "apptcancelyes", "apptcancel", "rebooksame"]
)
def test_the_old_ids_still_decode(action):
    appointment_id = str(uuid4())
    assert decode_action_id(f"{action}|{appointment_id}") == (action, appointment_id)


def test_builders_and_decoder_agree():
    reminder_id = uuid4()
    buttons = rt.reminder_buttons(reminder_id)
    assert [decode_action_id(bid) for bid, _ in buttons] == [
        (action, str(reminder_id)) for action in rt.REMINDER_ACTIONS
    ]
    assert [label for _, label in buttons] == ["Confirmar", "Cancelar", "Outro"]
    assert rt.button_payloads(buttons) == [bid for bid, _ in buttons]


def test_every_label_fits_a_whatsapp_button():
    labels = [rt.LABEL_CONFIRM, rt.LABEL_CANCEL, rt.LABEL_OTHER, rt.LABEL_DEPOSIT_RESCHEDULE]
    assert all(0 < len(label) <= MAX_BUTTON_LABEL_CHARS for label in labels)


def test_the_pix_paid_variant_keeps_its_trio_and_pix_scoped_ids():
    reminder_id, appointment_id = uuid4(), uuid4()
    assert rt.deposit_reminder_buttons(reminder_id, appointment_id) == [
        (f"remconfirm|{reminder_id}", "Confirmar"),
        (f"apptresched|{appointment_id}", "Reagendar"),
        (f"apptcancel|{appointment_id}", "Cancelar"),
    ]


# --------------------------------------------------------------------------
# The Meta submission sheet stays in step with the code (Task 4)
# --------------------------------------------------------------------------

META_DOC = Path(__file__).resolve().parent.parent / "docs" / "LEMBRETES_MODELOS_META.md"


def test_the_meta_sheet_matches_the_code():
    text = META_DOC.read_text(encoding="utf-8")
    settings = get_settings()

    assert f"`{settings.REMINDER_V2_TEMPLATE_NAME}`" in text
    assert f"`{settings.REMINDER_TEMPLATE_NAME}`" in text
    assert f"`{settings.REMINDER_DEPOSIT_TEMPLATE_NAME}`" in text

    body = next(line for line in text.splitlines() if line.startswith("> LEMBRE-SE:"))
    placeholders = re.findall(r"\{\{(\d+)\}\}", body)
    assert placeholders == [str(i) for i in range(1, rt.TEMPLATE_PARAM_COUNT + 1)]
    # Meta refuses a body that starts or ends with a variable.
    assert not body.removeprefix("> ").startswith("{{")
    assert not body.rstrip().endswith("}}")

    for label in (rt.LABEL_CONFIRM, rt.LABEL_CANCEL, rt.LABEL_OTHER):
        assert f"`{label}`" in text
    assert f"`{rt.NO_REQUIREMENTS_PARAM}`" in text
