"""The operational portal base supplies both staff and patient links."""

import pytest

from secretaria.config import Settings
from secretaria.services import reminder_text


@pytest.mark.parametrize("suffix", ["", "/", "///"])
def test_agenda_and_reminders_share_public_base(monkeypatch, suffix):
    settings = Settings(
        _env_file=None, BRAIN_MESSAGE_PUBLIC_URL=f" https://portal.example{suffix} "
    )
    monkeypatch.setattr(reminder_text, "get_settings", lambda: settings)
    assert settings.DOCTOR_AGENDA_URL == "https://portal.example/agenda"
    assert settings.PORTAL_POST_OAUTH_REDIRECT == "https://portal.example/contexto"
    assert reminder_text.portal_conversation_link("clinic", "reminder") == (
        "https://portal.example/clinicas/?convite=clinic&produto=secretaria&lembrete=reminder"
    )


def test_empty_public_base_omits_links(monkeypatch):
    settings = Settings(_env_file=None, BRAIN_MESSAGE_PUBLIC_URL="")
    monkeypatch.setattr(reminder_text, "get_settings", lambda: settings)
    assert settings.DOCTOR_AGENDA_URL == ""
    assert settings.PORTAL_POST_OAUTH_REDIRECT == ""
    assert reminder_text.portal_conversation_link("clinic") is None
