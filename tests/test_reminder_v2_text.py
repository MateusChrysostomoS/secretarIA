"""services/reminder_text.py — the words of the reminder (TASK-032 R2, spec §4.2)."""

from datetime import UTC, datetime

from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS
from secretaria.models import Appointment, Tenant
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
