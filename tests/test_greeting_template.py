"""Tests for services/greeting_template.py — the product greeting frame.

Two things are pinned here, and they fail for different reasons:

  - The frame's OBLIGATIONS (automated-assistant disclosure, no-medical-advice,
    emergency escape). These are the reason the greeting stopped being clinic
    free text at all, so they must survive every clinic's configuration —
    including the common one, which is "nothing configured".
  - The SIZE contract. The greeting always ships with action buttons, so the
    whole rendered message must fit WhatsApp's 1024-char interactive body, and
    `services/whatsapp.py::send_buttons` does not truncate: one character over
    is a 400 from Meta and the patient's first-ever message goes unanswered.
    The budget is asserted by RENDERING at the budget, never against a
    hardcoded number — a literal would pin a stale budget the moment the frame
    copy is edited, and pass while production overflowed.
"""

import os
import re

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import pytest  # noqa: E402

from secretaria.core.whatsapp_limits import (  # noqa: E402
    MAX_BUTTON_LABEL_CHARS,
    MAX_INTERACTIVE_BODY_CHARS,
)
from secretaria.services.greeting_template import (  # noqa: E402
    CONSENT_BUTTON_LABEL,
    LGPD_CONSENT_MESSAGE,
    LGPD_TERMS_URL,
    PREVIEW_PLACEHOLDER,
    clinic_description_budget,
    greeting_preview_template,
    render_greeting,
)
from secretaria.workers import tasks  # noqa: E402

# Deliberately spans the realistic range: an empty name (a half-provisioned
# tenant), a short one, and one long enough to eat a third of the slot.
CLINIC_NAMES = [
    "",
    "Clinic",
    "Clínica São Lucas",
    "Instituto de Oftalmologia Avançada de Belo Horizonte",
]


# --------------------------------------------------------------------------
# The obligations survive any configuration
# --------------------------------------------------------------------------


@pytest.mark.parametrize("description", ["", None, "   ", "Oftalmologia e cirurgia."])
def test_obligations_are_present_whatever_the_clinic_configured(description: str | None) -> None:
    rendered = render_greeting("Clínica São Lucas", description)

    assert "assistente virtual automatizado" in rendered
    assert "Nenhuma orientação médica é dada aqui" in rendered
    assert "Em emergência, não use este canal" in rendered


def test_description_is_rendered_into_the_slot() -> None:
    rendered = render_greeting("Clínica São Lucas", "Oftalmologia e cirurgia refrativa.")

    assert "Oftalmologia e cirurgia refrativa." in rendered
    # Placed above the disclosure, where the clinic's own pitch belongs.
    assert rendered.index("Oftalmologia e cirurgia") < rendered.index("🤖 Importante")


@pytest.mark.parametrize("description", ["", None, "  \n\n  "])
def test_empty_description_leaves_no_visible_gap(description: str | None) -> None:
    """The slot collapses WITH its blank lines, not just its text.

    Regression: the first implementation used `replace("\\n\\n\\n", "\\n\\n")`,
    which consumes only three of the four newlines an empty slot leaves, so
    every unconfigured clinic opened with a stray blank line.
    """
    rendered = render_greeting("Clinic", description)

    assert "\n\n\n" not in rendered


def test_missing_clinic_name_still_reads_grammatically() -> None:
    rendered = render_greeting(None, "Oftalmologia.")

    assert "nossa clínica" in rendered
    assert "{clinic_name}" not in rendered


# --------------------------------------------------------------------------
# The size contract
# --------------------------------------------------------------------------


@pytest.mark.parametrize("clinic_name", CLINIC_NAMES)
def test_a_description_at_the_budget_renders_exactly_at_the_cap(clinic_name: str) -> None:
    """The budget is EXACT, not approximate — the bug this pins was ±2.

    `clinic_description_budget` used to measure the frame with an EMPTY slot,
    but an empty slot collapses the two blank lines around it. The budget came
    back two characters too generous, so a description the hub accepted at
    exactly the cap rendered to 1026 chars and Meta rejected the send.
    """
    budget = clinic_description_budget(clinic_name)
    rendered = render_greeting(clinic_name, "x" * budget)

    assert len(rendered) == MAX_INTERACTIVE_BODY_CHARS


@pytest.mark.parametrize("clinic_name", CLINIC_NAMES)
def test_one_character_over_the_budget_overflows(clinic_name: str) -> None:
    """The companion to the test above: proves the budget is a real ceiling
    rather than a number that merely happens to sit under it."""
    budget = clinic_description_budget(clinic_name)
    rendered = render_greeting(clinic_name, "x" * (budget + 1))

    assert len(rendered) > MAX_INTERACTIVE_BODY_CHARS


def test_a_longer_clinic_name_leaves_a_smaller_budget() -> None:
    assert clinic_description_budget("Clinic") > clinic_description_budget(
        "Instituto de Oftalmologia Avançada de Belo Horizonte"
    )


# --------------------------------------------------------------------------
# The frame's promises must be true
# --------------------------------------------------------------------------


def test_the_voltar_promise_is_backed_by_a_real_command() -> None:
    """The frame tells every patient "Digite *voltar*".

    Before this round every menu command was slash-prefixed (`/menu`,
    `/reset`, …) — something no patient types — so this line would have been
    dead copy: an escape hatch advertised to patients that did nothing. This
    test fails if someone narrows `_MENU_COMMANDS` again without also editing
    the frame.
    """
    assert "Digite *voltar*" in render_greeting("Clinic", "")
    assert tasks.is_menu_command("voltar") is True


def test_ordinary_sentences_containing_voltar_are_not_menu_commands() -> None:
    """The cost of the line above, bounded: whole-body match only, so a patient
    mid-booking saying when they can come back still routes normally."""
    assert tasks.is_menu_command("quero voltar na segunda") is False


# --------------------------------------------------------------------------
# The LGPD notice
# --------------------------------------------------------------------------


def test_consent_notice_carries_the_terms_link() -> None:
    assert LGPD_TERMS_URL in LGPD_CONSENT_MESSAGE
    assert "LGPD" in LGPD_CONSENT_MESSAGE


def test_consent_notice_fits_an_interactive_body() -> None:
    """It is sent with its own button, so it lives under the same cap the
    greeting does."""
    assert len(LGPD_CONSENT_MESSAGE) <= MAX_INTERACTIVE_BODY_CHARS


def test_consent_button_label_fits_whatsapp() -> None:
    """WhatsApp rejects an over-long button title, which would take the whole
    consent message down with it."""
    assert len(CONSENT_BUTTON_LABEL) <= MAX_BUTTON_LABEL_CHARS


@pytest.mark.parametrize("body", ["✅ Concordo", "Concordo", "concordo", "  CONCORDO  "])
def test_consent_acceptance_matches_tap_and_typed_forms(body: str) -> None:
    """Matched through `strip_decoration`, like every other decorated label, so
    the emoji stays a render concern and a patient who types the word instead
    of tapping is honoured too."""
    assert tasks._is_consent_acceptance(body) is True


@pytest.mark.parametrize("body", ["agendar", "não concordo mesmo", "", None])
def test_consent_acceptance_ignores_everything_else(body: str | None) -> None:
    assert tasks._is_consent_acceptance(body) is False


# --------------------------------------------------------------------------
# The hub preview must BE the message, not resemble it
# --------------------------------------------------------------------------


@pytest.mark.parametrize("clinic_name", CLINIC_NAMES)
@pytest.mark.parametrize(
    "description",
    ["", "Oftalmologia e cirurgia refrativa.", "Atendemos convênios.\n\nEstacionamento."],
)
def test_preview_template_reconstitutes_the_real_message(
    clinic_name: str, description: str
) -> None:
    """The invariant the whole hub preview rests on.

    The frontend receives `greeting_preview_template`, splits it once on the
    placeholder, and renders `before + what-the-clinic-typed + after`. If that
    does not reproduce `render_greeting` byte for byte, the clinic approves one
    message and its patients receive a different one — the exact ambiguity this
    round exists to remove. Simulated here in the same order the frontend does
    it, INCLUDING its blank-run collapse (MessagesSection.tsx::collapseBlankRun),
    which is the one piece of logic mirrored on that side.
    """
    template = greeting_preview_template(clinic_name)
    before, _, after = template.partition(PREVIEW_PLACEHOLDER)

    as_the_hub_shows_it = re.sub(r"\n{3,}", "\n\n", before + description.strip() + after)

    assert as_the_hub_shows_it == render_greeting(clinic_name, description)


def test_preview_template_contains_exactly_one_placeholder() -> None:
    """A second occurrence would make the frontend's single `split` drop copy
    without any error — the preview would silently lose a paragraph."""
    template = greeting_preview_template("Clínica São Lucas")

    assert template.count(PREVIEW_PLACEHOLDER) == 1
