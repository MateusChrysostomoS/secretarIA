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

import hashlib
import os
import re
from types import SimpleNamespace

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
from secretaria.models.patient import CHANNEL_WHATSAPP  # noqa: E402
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE  # noqa: E402
from secretaria.services.greeting_template import (  # noqa: E402
    CONSENT_BUTTON_LABEL,
    FRAME_FIXED_CHARS,
    LGPD_CONSENT_MESSAGE,
    LGPD_TERMS_URL,
    PREVIEW_PLACEHOLDER,
    clinic_description_budget,
    greeting_preview_template,
    render_greeting,
    tenant_city,
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

    # Comma after the name, first letter lower-cased to continue the sentence.
    assert (
        "Sou a SecretarIA, assistente virtual da Clínica São Lucas, "
        "oftalmologia e cirurgia refrativa.\n"
    ) in rendered
    assert rendered.index("oftalmologia e cirurgia") < rendered.index("🤖 Importante")


def test_city_closes_the_identification_sentence() -> None:
    rendered = render_greeting(
        "Chrysostomo For Eyes",
        "Clínica especializada em saúde ocular, cirurgia refrativa e catarata.",
        city="Belo Horizonte",
    )

    assert (
        "Sou a SecretarIA, assistente virtual da Chrysostomo For Eyes, "
        "clínica especializada em saúde ocular, cirurgia refrativa e catarata, "
        "em Belo Horizonte.\n"
    ) in rendered


def test_city_goes_after_a_multi_sentence_description_and_the_period_stays_last() -> None:
    rendered = render_greeting(
        "Clinic", "Clínica de olhos. Atendimento particular e por convênio.", city="Recife"
    )

    assert "convênio, em Recife.\n" in rendered


def test_city_without_description() -> None:
    rendered = render_greeting("Clinic", "", city="Recife")

    assert "assistente virtual da Clinic, em Recife.\n" in rendered


@pytest.mark.parametrize("address", [None, {}, {"city": ""}, {"city": None}, "x", {"city": 3}])
def test_tenant_city_ignores_missing_or_odd_values(address: object) -> None:
    assert tenant_city(address) == ""
    assert "em " not in render_greeting("Clinic", "", city=tenant_city(address)).split("\n")[1]


def test_tenant_city_reads_and_strips_the_address_city() -> None:
    assert tenant_city({"city": "  Belo Horizonte ", "state": "MG"}) == "Belo Horizonte"


@pytest.mark.parametrize(
    ("description", "expected_start"),
    [
        ("Clínica de olhos", "clínica de olhos"),
        ("CRM-based clinic", "CRM-based clinic"),
        ("iPhone clinic", "iPhone clinic"),
        ("clínica já minúscula", "clínica já minúscula"),
    ],
)
def test_first_letter_is_lowered_only_for_ordinary_words(
    description: str, expected_start: str
) -> None:
    rendered = render_greeting("Clinic", description)

    assert f"da Clinic, {expected_start}.\n" in rendered


@pytest.mark.parametrize("city", ["", "Belo Horizonte", "São José dos Campos"])
@pytest.mark.parametrize("clinic_name", CLINIC_NAMES)
def test_budget_stays_exact_with_a_city(clinic_name: str, city: str) -> None:
    budget = clinic_description_budget(clinic_name, city)

    at_budget = render_greeting(clinic_name, "x" * budget, city=city)
    over_budget = render_greeting(clinic_name, "x" * (budget + 1), city=city)
    assert len(at_budget) == MAX_INTERACTIVE_BODY_CHARS
    assert len(over_budget) > MAX_INTERACTIVE_BODY_CHARS


def test_empty_description_leaves_no_trailing_space_on_the_identification_line() -> None:
    rendered = render_greeting("Clínica São Lucas", "")

    assert "da Clínica São Lucas.\n" in rendered
    assert " \n" not in rendered


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


@pytest.mark.parametrize("city", ["", "Belo Horizonte"])
@pytest.mark.parametrize("clinic_name", CLINIC_NAMES)
@pytest.mark.parametrize(
    "description",
    # No empty description here: the hub shows its "(sua descrição aparece aqui)"
    # hint in the slot instead, so the comma before it is expected in the preview.
    ["oftalmologia e cirurgia refrativa", "atendemos convênios.\n\nEstacionamento"],
)
def test_preview_template_reconstitutes_the_real_message(
    clinic_name: str, description: str, city: str
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
    # Descriptions here start lower-case and carry no closing punctuation: the
    # backend also lower-cases the first letter and moves the closing
    # punctuation after the city, two touch-ups the frontend preview does not
    # mirror (see the tests below for the real message).
    template = greeting_preview_template(clinic_name, city)
    before, _, after = template.partition(PREVIEW_PLACEHOLDER)

    # The description now sits inline at the end of the identification line, so
    # an empty one leaves a dangling separator space; the backend drops it (a
    # trailing space is invisible in the hub's pre-wrap preview).
    as_the_hub_shows_it = re.sub(r"[ \t]+\n", "\n", before + description.strip() + after)
    as_the_hub_shows_it = re.sub(r"\n{3,}", "\n\n", as_the_hub_shows_it)

    assert as_the_hub_shows_it == render_greeting(clinic_name, description, city=city)


def test_preview_template_contains_exactly_one_placeholder() -> None:
    """A second occurrence would make the frontend's single `split` drop copy
    without any error — the preview would silently lose a paragraph."""
    template = greeting_preview_template("Clínica São Lucas")

    assert template.count(PREVIEW_PLACEHOLDER) == 1


# --------------------------------------------------------------------------
# Patient name in the opener (Portal only)
# --------------------------------------------------------------------------

# Captured from `render_greeting("GinecoAqui", "")` / the preview / the budget
# after the opener became "Olá! Bem-vindo(a) ao/à <nome>!" and the second line
# "Sou a SecretarIA, assistente virtual do/da <nome>, <descrição>." (originally
# pinned at HEAD 7ae484d, before
# the `patient_name` slot existed). The no-name path must stay byte-for-byte
# identical: WhatsApp always takes it, and its 1024-char budget is measured on it.
_NO_NAME_GREETING_SHA256 = "697a88382729e9192d05fceccca6873700d5941803ed0b97f8a7affe8856c17b"
_NO_NAME_GREETING_LEN = 783
_PREVIEW_SHA256 = "36280800e15176441547e94644c05be52bc0e9d91ce4f8d34c89a58ec50fbdff"
_GINECOAQUI_BUDGET = 239


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_greeting_with_patient_name_addresses_the_patient() -> None:
    rendered = render_greeting("GinecoAqui", "", patient_name="Mateus")

    assert rendered.startswith("👋 Olá, Mateus! Bem-vindo(a) à GinecoAqui!\n")
    assert "Olá, Mateus!" in rendered


@pytest.mark.parametrize("patient_name", [None, "", "   "])
def test_greeting_without_patient_name_is_unchanged(patient_name: str | None) -> None:
    rendered = render_greeting("GinecoAqui", "", patient_name=patient_name)

    assert len(rendered) == _NO_NAME_GREETING_LEN
    assert _sha256(rendered) == _NO_NAME_GREETING_SHA256
    assert rendered == render_greeting("GinecoAqui", "")
    assert ", !" not in rendered


def test_budget_and_preview_unchanged_by_patient_name_slot() -> None:
    assert clinic_description_budget("GinecoAqui") == _GINECOAQUI_BUDGET
    assert FRAME_FIXED_CHARS == len(render_greeting("", ""))
    assert _sha256(greeting_preview_template("GinecoAqui")) == _PREVIEW_SHA256


@pytest.mark.parametrize(
    ("channel", "expect_name"),
    [(CHANNEL_BRAIN_MESSAGE, True), (CHANNEL_WHATSAPP, False)],
)
def test_select_greeting_names_patient_only_on_portal(channel: str, expect_name: bool) -> None:
    tenant = SimpleNamespace(
        clinic_name="GinecoAqui",
        clinic_description="",
        address={"city": "Belo Horizonte"},
        returning_greeting_message=None,
    )
    patient = SimpleNamespace(name="Mateus", channel=channel)

    greeting = tasks._select_greeting(
        tenant, patient, is_first_contact=True, is_returning_patient=False
    )

    assert greeting is not None
    assert ("Olá, Mateus!" in greeting) is expect_name
    if not expect_name:
        assert greeting == render_greeting("GinecoAqui", "", city="Belo Horizonte")


# --------------------------------------------------------------------------
# Instituto Gicovate feedback (2026-11-09)
# --------------------------------------------------------------------------


def test_masculine_clinic_name_takes_ao_and_do() -> None:
    rendered = render_greeting("Instituto Gicovate", "Cirurgia do aparelho digestivo.")

    assert rendered.startswith("👋 Olá! Bem-vindo(a) ao Instituto Gicovate!\n")
    assert "assistente virtual do Instituto Gicovate, cirurgia do aparelho digestivo.\n" in rendered


def test_feminine_or_brand_name_keeps_a_and_da() -> None:
    rendered = render_greeting("Clínica Vignolli", "")

    assert rendered.startswith("👋 Olá! Bem-vindo(a) à Clínica Vignolli!\n")
    assert "assistente virtual da Clínica Vignolli.\n" in rendered


def test_city_already_in_description_is_not_repeated() -> None:
    rendered = render_greeting(
        "Instituto Gicovate",
        "Cirurgia do aparelho digestivo com o Dr. Carlos Gicovate Neto, em Campos dos Goytacazes.",
        city="Campos dos Goytacazes",
    )

    assert rendered.count("Campos dos Goytacazes") == 1
    assert "Neto, em Campos dos Goytacazes.\n" in rendered


def test_city_match_ignores_case_and_accents() -> None:
    rendered = render_greeting("Clínica X", "Atendemos em sao paulo", city="São Paulo")

    assert "em São Paulo" not in rendered
    assert "atendemos em sao paulo.\n" in rendered
