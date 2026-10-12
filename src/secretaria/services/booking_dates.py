"""Resolve date expressions against the clinic's clock, never the server's day.

This is calendar arithmetic, not availability. The booking resolver still rejects
past/out-of-window dates and reads the chosen professional's actual free slots.
"""

import re
import unicodedata
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
_WEEKDAY_NUMBERS = {
    "segunda": 0, "terca": 1, "quarta": 2, "quinta": 3,
    "sexta": 4, "sabado": 5, "domingo": 6,
}


_HHMM = re.compile(r"(\d{1,2}):(\d{2})")


def clinic_today(timezone: str, *, now: datetime | None = None) -> date:
    instant = now if now is not None else datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return instant.astimezone(ZoneInfo(timezone)).date()


def time_has_passed_today(
    day: date, at: time | str | None, *, timezone: str, now: datetime | None = None
) -> bool:
    """True when `day` is the clinic's today and `at` (a time or "HH:MM") is already past.

    Owner, 2026-10-07: "quarta às 10" said on a Wednesday after 10h means NEXT Wednesday.
    """
    if at is None or day != clinic_today(timezone, now=now):
        return False
    if isinstance(at, str):
        match = _HHMM.fullmatch(at.strip())
        if not match:
            return False
        at = time(int(match.group(1)), int(match.group(2)))
    instant = now if now is not None else datetime.now(UTC)
    return at <= instant.astimezone(ZoneInfo(timezone)).time()


def resolve_booking_day(
    text: str, *, timezone: str, now: datetime | None = None, at: str | None = None
) -> date:
    """Canonical date or ValueError for an invalid/ambiguous expression.

    A date without a year uses the current clinic year. It does not silently
    roll a past date into next year. 'Semana que vem' is next Monday–Sunday;
    a bare weekday is its nearest occurrence, including today - unless `at` (the
    requested "HH:MM") has already passed today, then it is next week's (owner,
    2026-10-07: "quarta às 10" said on a Wednesday at 11h is next Wednesday).
    """
    raw = text.strip().casefold()
    today = clinic_today(timezone, now=now)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return date.fromisoformat(raw)
    explicit = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", raw)
    if explicit:
        day, month, year = explicit.groups()
        return date(int(year) if year else today.year, int(month), int(day))
    normalized = "".join(
        c for c in unicodedata.normalize("NFKD", raw) if not unicodedata.combining(c)
    )
    normalized = " ".join(normalized.split())
    relative = {"hoje": 0, "amanha": 1, "depois de amanha": 2}
    if normalized in relative:
        return today + timedelta(days=relative[normalized])
    after = re.fullmatch(r"daqui a (\d{1,3}) dias?", normalized)
    if after:
        return today + timedelta(days=int(after.group(1)))
    weekday = re.fullmatch(
        r"(segunda|terca|quarta|quinta|sexta|sabado|domingo)(?:-feira)?"
        r"( da semana que vem| da proxima semana)?", normalized,
    )
    if weekday:
        day_number = _WEEKDAY_NUMBERS[weekday.group(1)]
        if weekday.group(2):
            return today - timedelta(days=today.weekday()) + timedelta(days=7 + day_number)
        nearest = today + timedelta(days=(day_number - today.weekday()) % 7)
        if time_has_passed_today(nearest, at, timezone=timezone, now=now):
            nearest += timedelta(days=7)
        return nearest
    raise ValueError("invalid or ambiguous booking date")


def date_context(timezone: str, *, now: datetime | None = None) -> str:
    """Fresh, bounded calendar reference for natural-language interpretation."""
    today = clinic_today(timezone, now=now)
    lines = [
        f"- Hoje é {today.isoformat()} ({WEEKDAYS[today.weekday()]}, timezone {timezone}).",
        "- DATAS: use o calendário abaixo, não a data do servidor nem exemplos antigos.",
        "  amanhã = hoje + 1 dia; depois de amanhã = hoje + 2 dias.",
        "  'quinta da semana que vem' é a quinta da próxima semana de segunda a domingo.",
        "  Um dia da semana igual ao de hoje (ex.: 'quarta' numa quarta) é HOJE só se o "
        "horário pedido ainda não passou; se já passou, é o mesmo dia da semana que vem.",
        "  DD/MM sem ano usa o ano de hoje na clínica; "
        "não mude para o próximo ano silenciosamente.",
        "  Se a data for inválida, passada ou ambígua, peça esclarecimento; não adivinhe.",
        "  Converta para AAAA-MM-DD em day. Não invente um horário que o paciente não informou.",
        "  Estes dias são referência de calendário, não uma promessa de disponibilidade.",
        "  PRÓXIMOS DIAS:",
    ]
    for offset in range(20):
        day = today + timedelta(days=offset)
        lines.append(f"  {day.isoformat()} — {WEEKDAYS[day.weekday()]}")
    return "\n".join(lines) + "\n"
