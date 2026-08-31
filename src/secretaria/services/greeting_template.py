"""The product-defined first-contact greeting frame, and the LGPD consent notice.

As of this round the first-contact greeting is NO LONGER the clinic's own free
text. It is a FIXED product template (`GREETING_FRAME`) with exactly two
clinic-supplied slots:

  - ``{clinic_name}``        -> ``Tenant.clinic_name`` (already collected at
    onboarding; never typed into a greeting field).
  - ``{clinic_description}`` -> ``Tenant.clinic_description``, the ONLY part a
    clinic edits in the hub: what it offers, its values, a differentiator.

This mirrors the move already made for the greeting's buttons (see
``docs/CHECKPOINT_fixed_greeting_buttons.md``): the parts that carry product
obligations — disclosing that the assistant is automated, that no medical
advice is given here, how to use the buttons, the emergency escape — cannot be
left to per-clinic free text, because a clinic that never fills the field (the
common case: the column defaults to NULL) would ship none of them.

``Tenant.greeting_message`` is ORPHANED by the same logic, and for the same
reason ``greeting_buttons`` was: reinterpreting a column that today holds a
WHOLE greeting as "just the description" would render the clinic's own
"Olá! Sou a secretária…" INSIDE a frame that already says exactly that — the
duplicated, ambiguous opener this round exists to remove.

Everything here is pure string work: no DB, no ORM, no clock. The worker
renders it (``workers/tasks.py::_select_greeting``) and the hub both validates
against it and serves the rendered preview the frontend shows the clinic
(``schemas/config.py``, ``services/hub_configuration.py``), so the copy is
written down ONCE and the preview can never drift from what is actually sent.
"""

import re

from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS

# The LGPD terms the patient is pointed at. Product-level and identical for
# every clinic (the controller relationship is Brain Co's, not the clinic's),
# so it is a constant here rather than a per-tenant column.
LGPD_TERMS_URL = (
    "https://docs.google.com/document/d/"
    "1-eQvCuKyJ3a4wP0K5Q-YrYJUlSYEdeA6ceRgt8KBUlY/edit?usp=sharing"
)

# Message 2, sent immediately after the greeting on a patient's first contact.
# Carries ONE button (`CONSENT_BUTTON_LABEL`); tapping it records a
# ConsentEvent — see workers/tasks.py::_send_consent_notice.
LGPD_CONSENT_MESSAGE = (
    "✅ Antes de continuarmos, preciso confirmar sua concordância com os Termos "
    "de Uso e a Política de Privacidade, garantindo a proteção dos seus dados "
    "conforme a LGPD.\n\n"
    f"📄 Leia aqui: {LGPD_TERMS_URL}\n\n"
    "✍️ Para seguir, clique no botão abaixo:"
)

# Kept inside WhatsApp's button-label cap (core/whatsapp_limits.py) on purpose:
# it is matched by its LABEL, like every other deterministic tap in this
# codebase (see schemas/webhook.py::extract_inbound_body, which returns a
# plain button's title, not its id).
CONSENT_BUTTON_LABEL = "✅ Concordo"

# Sent right after the tap, so the acceptance is visibly acknowledged instead
# of the patient wondering whether the button worked.
CONSENT_ACCEPTED_MESSAGE = (
    "Perfeito, obrigado! ✅ Sua concordância foi registrada.\n\n"
    "Agora me diga: como posso ajudar você hoje?"
)

# The ConsentEvent.kind written when the button is tapped. `first_contact_service`
# (models/consent_event.py) already records patient creation; this is the
# distinct, EXPLICIT acceptance moment and must not be folded into it.
CONSENT_EVENT_KIND = "terms_accepted"

# ---------------------------------------------------------------------------
# The frame
# ---------------------------------------------------------------------------
# Written as ONE literal so what a reviewer reads is what the patient receives.
# `{clinic_description}` sits high (right after the identification) because it
# is the part the clinic controls and the part a patient actually scans;
# everything below it is obligation copy whose order is deliberate:
# automated-assistant disclosure -> what the bot can do -> how to talk to it ->
# emergency escape -> the invitation to start.
#
# The "Digite *voltar*" promise is load-bearing, not decoration: it is only
# true because `voltar` is a real menu command (workers/tasks.py::_MENU_COMMANDS,
# which this round widened from slash-only). Never edit that line without
# checking the command still exists — advertising an escape hatch that does
# nothing is worse than not advertising one.
GREETING_FRAME = """\
👋 Olá! Bem-vindo(a) à {clinic_name}!
Sou a secretária virtual e cuido dos agendamentos por aqui. 😊

{clinic_description}

🤖 Importante: você conversa com um *assistente virtual automatizado*, que usa \
inteligência artificial para organizar o seu atendimento. Nenhuma orientação \
médica é dada aqui — dúvidas sobre a sua saúde são respondidas pela equipe da \
clínica.

Posso te ajudar com:

📅 Agendar consulta, exame ou procedimento
🔄 Remarcar ou cancelar um agendamento
ℹ️ Horários, endereço, valores e preparo

Para eu te atender rápido:

⚠️ Faça um pedido por vez, em uma única mensagem.

⚠️ Quando aparecerem botões, clique no botão em vez de digitar — evita erro no \
agendamento.

⚠️ Errou? Digite *voltar* a qualquer momento para recomeçar.

🚨 Em emergência, não use este canal: procure o pronto-socorro ou ligue 192.

✨ Vamos começar? Me diga o que você precisa!"""


def render_greeting(clinic_name: str | None, clinic_description: str | None) -> str:
    """Render the frame for one tenant. Pure; never raises on missing input.

    An empty `clinic_description` collapses cleanly: the blank line that would
    surround it goes too, so a clinic that never filled the field still gets a
    well-formed greeting rather than a visible hole. An empty `clinic_name`
    degrades to a generic but grammatical opener rather than printing an empty
    placeholder.
    """
    name = (clinic_name or "").strip()
    description = (clinic_description or "").strip()
    rendered = GREETING_FRAME.format(
        clinic_name=name or "nossa clínica",
        clinic_description=description,
    )
    # Collapse the run of blank lines an empty slot leaves behind. Done with a
    # regex, not `replace("\n\n\n", "\n\n")`: an empty slot leaves FOUR
    # newlines, and `str.replace` consumes only the first three of them, so the
    # greeting still opened with a visible extra blank line. Applied
    # unconditionally, so a description typed with trailing blank lines cannot
    # reintroduce the gap either.
    return re.sub(r"\n{3,}", "\n\n", rendered)


# A single non-blank character, used only to measure the frame's true cost
# (see `clinic_description_budget`). Any one-character string works; a
# printable one keeps the probe render readable when debugging.
_BUDGET_PROBE = "x"

# What the frame costs with both slots empty — i.e. what a clinic that
# configured nothing actually sends. DERIVED, never hand-counted: the literal
# above will be edited over time, and a stale number here would silently hand
# the clinic a budget that overflows the send. NOTE this is NOT the number the
# budget is computed from — an empty slot collapses its own blank lines, so it
# understates the frame by two characters. See `clinic_description_budget`.
FRAME_FIXED_CHARS = len(render_greeting("", ""))


def clinic_description_budget(clinic_name: str | None) -> int:
    """How many characters of description still fit for THIS clinic.

    The greeting always ships with action buttons attached, so the whole
    rendered message must fit WhatsApp's interactive-body cap (1024), not the
    4096-char plain-text one — see schemas/config.py's greeting validator and
    services/whatsapp.py::send_buttons, which does NOT truncate the body: an
    over-cap greeting is a 400 from Meta, and `_send_greeting` logs it and
    moves on, so the patient receives NOTHING at all.

    The budget therefore depends on the clinic's own name length, which is why
    this is a function and not a constant. Never negative.

    Measured against a ONE-CHARACTER probe rather than an empty slot, and that
    is not a nicety: an empty slot makes `render_greeting` collapse the two
    blank lines around it, so measuring there hands back a budget two
    characters too generous — enough to push a description that the hub
    accepted at exactly the cap to 1026 chars on the wire, which Meta rejects
    outright. Subtracting the probe leaves the true cost of the frame
    INCLUDING the separators a real description reinstates.
    """
    probe = render_greeting(clinic_name, _BUDGET_PROBE)
    frame_cost = len(probe) - len(_BUDGET_PROBE)
    return max(0, MAX_INTERACTIVE_BODY_CHARS - frame_cost)


# The token the hub's live preview splits on. The frontend must show the clinic
# what its patients will receive WHILE it types, which a fully-rendered string
# cannot do — but re-typing 800+ characters of frame copy in TypeScript would
# guarantee the preview and the real message drift apart the first time either
# is edited. So the frame is served ONCE, with this token marking the slot, and
# the frontend does a single `split` on it.
PREVIEW_PLACEHOLDER = "{{descricao}}"


def greeting_preview_template(clinic_name: str | None) -> str:
    """The frame for this clinic with `PREVIEW_PLACEHOLDER` in the slot.

    Byte-for-byte what `render_greeting` produces, except the clinic's own text
    is replaced by the token — so whatever the hub renders around the token IS
    the message the patient receives, including the clinic's name.
    """
    return render_greeting(clinic_name, PREVIEW_PLACEHOLDER)
