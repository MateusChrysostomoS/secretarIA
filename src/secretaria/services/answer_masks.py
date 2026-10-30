"""Fixed stand-ins stored in place of an answer that must never become a `messages` row.

Two answers are secrets of the patient's, not conversation content: the six-digit login code
(Portal, while the chat waits for it) and the CPF / e-mail the payment provider asks for. The
worker keeps the real value only in the turn's memory and stores one of these instead, so the
clinic console, the patient's own transcript and the AI history all read a row that says "the
patient answered" without saying what.

Bullets, never asterisks: the channels' text formatting reads `*...*` as bold, so a run of
asterisks would not draw as what it is. The same two strings are repeated in the Portal
(Brain-Message-Frontend `lib/answer-masks.ts`), which uses them to fold the bubble the patient
sees at the moment of sending into the stored one - change them in both places or not at all.
"""

# The login code: always six bullets, whatever was typed (never the typed shape).
CODE_MASK = "••••••"
# The payer data (CPF, or the e-mail a provider such as Mercado Pago needs): one length for every
# answer, so a mistyped document does not show its mistake in the history either.
PAYER_DATA_MASK = "•••••••••••"

ANSWER_MASKS = frozenset({CODE_MASK, PAYER_DATA_MASK})
