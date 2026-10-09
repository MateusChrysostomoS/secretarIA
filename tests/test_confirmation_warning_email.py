"""The clinic warning e-mail and the Portal nudge template (TASK-032 R4, spec 4.4)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from secretaria.services import email as email_module  # noqa: E402

KWARGS = dict(
    clinic_name="Clínica Olhar",
    warn_kind="unconfirmed",
    patient_label="Maria Souza",
    professional_name="Dra. Ana",
    service_name="Consulta",
    when_text="08/10/2026 às 09:00",
    reminder_label="lembrete de 1 dia antes",
    agenda_link="https://app.exemplo/agenda?consulta=abc",
)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    sent: list[tuple] = []
    monkeypatch.setattr(
        email_module, "get_settings", lambda: SimpleNamespace(SMTP_HOST="smtp.example.com")
    )
    monkeypatch.setattr(email_module, "_send_sync", lambda *args: sent.append(args))
    return sent


async def test_unconfirmed_warning_names_everything_the_clinic_needs(smtp):
    ok = await email_module.send_confirmation_warning_alert("contato@clinica.example", **KWARGS)

    assert ok is True
    (to, subject, body) = smtp[0]
    assert to == "contato@clinica.example"
    assert subject == "[SecretarIA] Consulta sem confirmação — Clínica Olhar"
    for needle in (
        "Maria Souza",
        "Dra. Ana",
        "Consulta",
        "08/10/2026 às 09:00",
        "lembrete de 1 dia antes",
        "https://app.exemplo/agenda?consulta=abc",
        "liberar o horário",
    ):
        assert needle in body
    assert "None" not in body


async def test_delivery_failed_variant_says_the_reminder_did_not_arrive(smtp):
    await email_module.send_confirmation_warning_alert(
        "contato@clinica.example", **{**KWARGS, "warn_kind": "delivery_failed"}
    )

    (_to, subject, body) = smtp[0]
    assert subject == "[SecretarIA] Lembrete não entregue — Clínica Olhar"
    assert "não foi possível entregar" in body.lower()


async def test_without_an_agenda_link_the_body_has_no_dangling_label(smtp):
    await email_module.send_confirmation_warning_alert(
        "contato@clinica.example", **{**KWARGS, "agenda_link": None}
    )

    body = smtp[0][2]
    assert "None" not in body and "agenda?consulta" not in body


async def test_returns_false_and_never_raises_when_smtp_is_off(monkeypatch):
    monkeypatch.setattr(email_module, "get_settings", lambda: SimpleNamespace(SMTP_HOST=""))

    assert await email_module.send_confirmation_warning_alert("a@b.c", **KWARGS) is False


async def test_returns_false_and_never_raises_when_the_send_breaks(monkeypatch):
    monkeypatch.setattr(
        email_module, "get_settings", lambda: SimpleNamespace(SMTP_HOST="smtp.example.com")
    )

    def _boom(*_args):
        raise OSError("connection refused")

    monkeypatch.setattr(email_module, "_send_sync", _boom)

    assert await email_module.send_confirmation_warning_alert("a@b.c", **KWARGS) is False


def test_portal_nudge_template_is_registered_and_renders_without_the_message_text():
    assert email_module.is_known_template("clinic_message_patient")
    tpl = email_module._TEMPLATES["clinic_message_patient"]
    variables = {"clinic_name": "Clínica Olhar", "link_line": "https://portal.exemplo/c"}

    subject = tpl.subject.format_map(variables)
    body = tpl.body.format_map(variables)

    assert "Clínica Olhar" in subject
    assert "https://portal.exemplo/c" in body
    assert "{" not in body
