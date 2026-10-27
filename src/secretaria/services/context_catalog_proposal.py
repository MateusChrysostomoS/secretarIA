"""The catalog side of a context source: professionals, services and convênios (TASK-067).

TASK-026 only read the clinic's free-text facts. A source that lists the doctors, what each one
does and which plans the clinic takes was thrown away. This module reads that too, with the same
discipline as the rest of the extraction:

  * every item carries a quote that is LITERALLY in the source; one without it is dropped;
  * the numbers, links and e-mails of a value must be in its quote (`grounded`), and the days of
    a doctor's week must be named in the quote - an item loses the part that is not backed, not
    the whole item;
  * nothing here writes. The result is a proposal; the screen applies it through the hub's own
    endpoints, and only into what the clinic left empty.

Whenever the code (or the model) had to fill a gap with its own judgement - one price for two
doctors, "todos os convênios" for lack of a per-doctor rule - it says so in `notes`, so the clinic
reads what was decided for it instead of finding out later.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from secretaria.schemas.config import WEEKDAYS, TimeWindow, _validate_business_hours
from secretaria.services.service_catalog import normalize as normalize_service

MAX_PROFESSIONALS = 30
MAX_SERVICES = 60
MAX_PLANS = 30
MAX_MODEL_NOTES = 10
MAX_NOTE_CHARS = 300
MAX_PRICE_CHARS = 40

# ------------------------------------------------------------------ what the model returns
# No `max_length` on purpose (see context_extraction): sizes are enforced afterwards.


class ExtractedHours(BaseModel):
    days: list[str] = Field(default_factory=list)  # monday .. sunday
    start: str  # "HH:MM"
    end: str  # "HH:MM"


class ExtractedProfessional(BaseModel):
    name: str
    specialty: str | None = None
    about: str | None = None
    email: str | None = None
    hours: list[ExtractedHours] = Field(default_factory=list)
    # The sentence that states the days and times, when it is not the one in `evidence`.
    hours_evidence: str | None = None
    evidence: str


class ExtractedService(BaseModel):
    name: str
    duration_min: int | None = None
    price: str | None = None  # as written: "R$ 400,00", "Gratuito"
    description: str | None = None
    requirements: list[str] = Field(default_factory=list)
    professional_names: list[str] = Field(default_factory=list)
    all_professionals: bool = False
    evidence: str


class ExtractedInsurancePlan(BaseModel):
    name: str
    how_it_works: str | None = None
    evidence: str


# ----------------------------------------------------------------------- text helpers


def fold(text: str | None) -> str:
    """Lowercase, accent-free, single-spaced: how names and days are compared."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", plain).strip().casefold()


_TITLES = {"dr", "dra", "doutor", "doutora", "prof", "profa", "professor", "professora"}


def _name_tokens(name: str | None) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", fold(name))
    return frozenset(w for w in words if w not in _TITLES)


def match_name(candidate: str | None, known: list[str]) -> int | None:
    """Index of the ONE known name `candidate` points at, or None (no match, or ambiguous).

    "Dr. Carlos" matches "Dr. Carlos Mendes"; two Carlos match neither.
    """
    wanted = _name_tokens(candidate)
    if not wanted:
        return None
    hits = []
    for index, name in enumerate(known):
        tokens = _name_tokens(name)
        if tokens and (wanted <= tokens or tokens <= wanted):
            hits.append(index)
    return hits[0] if len(hits) == 1 else None


def _significant(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", fold(name)) if len(t) >= 4 and t not in _TITLES]


# ----------------------------------------------------------------------------- weekdays

_DAY_WORDS = {
    "segunda": "monday",
    "terca": "tuesday",
    "quarta": "wednesday",
    "quinta": "thursday",
    "sexta": "friday",
    "sabado": "saturday",
    "domingo": "sunday",
}
_DAY = r"(segunda|terca|quarta|quinta|sexta|sabado|domingo)(?:-feira)?s?"
_RANGE = re.compile(rf"\b{_DAY}\s+(?:a|ate)\s+{_DAY}\b")
_SINGLE = re.compile(rf"\b{_DAY}\b")


def weekdays_named_in(text: str) -> set[str]:
    """The weekdays a sentence names: "segundas e quartas", "de segunda a sexta", "dias úteis"."""
    folded = fold(text)
    found: set[str] = set()
    for match in _RANGE.finditer(folded):
        first, last = _DAY_WORDS[match.group(1)], _DAY_WORDS[match.group(2)]
        start, end = WEEKDAYS.index(first), WEEKDAYS.index(last)
        if start <= end:
            found.update(WEEKDAYS[start : end + 1])
    for match in _SINGLE.finditer(folded):
        found.add(_DAY_WORDS[match.group(1)])
    if "dias uteis" in folded:
        found.update(WEEKDAYS[:5])
    if "todos os dias" in folded:
        found.update(WEEKDAYS)
    return found


def _ints(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"\d+", text)}


def _clock_in(clock: str, evidence_ints: set[int]) -> bool:
    match = re.fullmatch(r"(\d{2}):(\d{2})", clock or "")
    if not match:
        return False
    hour, minute = int(match.group(1)), int(match.group(2))
    return hour in evidence_ints and (minute == 0 or minute in evidence_ints)


Week = dict[str, list[dict[str, str]]]


def week_from_groups(groups: list[ExtractedHours]) -> Week:
    week: Week = {}
    for group in groups:
        for day in group.days:
            window = {"start": group.start, "end": group.end}
            if window not in week.setdefault(day, []):
                week[day].append(window)
    return {day: sorted(week[day], key=lambda w: w["start"]) for day in WEEKDAYS if day in week}


def verified_hours(hours: list[ExtractedHours], evidence: str) -> Week | None:
    """The week the evidence really states, in the hub's shape - or None when nothing holds."""
    allowed = weekdays_named_in(evidence)
    numbers = _ints(evidence)
    kept = [
        ExtractedHours(days=[d for d in g.days if d in allowed], start=g.start, end=g.end)
        for g in hours
        if _clock_in(g.start, numbers) and _clock_in(g.end, numbers)
    ]
    week = week_from_groups([g for g in kept if g.days])
    if not week:
        return None
    try:  # the exact rule the hub applies on save: an invalid week must not reach the screen
        _validate_business_hours({d: [TimeWindow(**w) for w in ws] for d, ws in week.items()})
    except (ValidationError, ValueError):
        return None
    return week


# ------------------------------------------------------------------------- verification


def _supported(evidence: str | None, source_norm: str) -> bool:
    quote = re.sub(r"\s+", " ", evidence or "").strip().casefold()
    return len(quote) >= 6 and quote in source_norm


def _grounded(text: str, evidence: str) -> bool:
    from secretaria.services.context_extraction import grounded

    return grounded(text, evidence)


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def verify_professional(p: ExtractedProfessional, source_norm: str) -> ExtractedProfessional | None:
    """The professional with only the parts their quote backs; None if the quote is not real."""
    if not _supported(p.evidence, source_norm):
        return None
    if not any(token in fold(p.evidence) for token in _name_tokens(p.name)):
        return None  # the quote is about somebody else
    specialty, about, email = _clean(p.specialty), _clean(p.about), _clean(p.email)
    if specialty and not _grounded(specialty, p.evidence):
        specialty = None
    if about and not _grounded(about, p.evidence):
        about = None
    if email and not (_grounded(email, p.evidence) and email.casefold() in p.evidence.casefold()):
        email = None
    hours_evidence = p.hours_evidence if _supported(p.hours_evidence, source_norm) else p.evidence
    week = verified_hours(p.hours, hours_evidence)
    return p.model_copy(
        update={
            "name": p.name.strip(),
            "specialty": specialty,
            "about": about,
            "email": email,
            "hours": [
                ExtractedHours(days=[day], start=w["start"], end=w["end"])
                for day, windows in (week or {}).items()
                for w in windows
            ],
            "hours_evidence": hours_evidence,
        }
    )


def verify_service(s: ExtractedService, source_norm: str) -> ExtractedService | None:
    if not _supported(s.evidence, source_norm):
        return None
    evidence_fold = fold(s.evidence)
    if not any(token in evidence_fold for token in _significant(s.name)):
        return None
    duration = s.duration_min if s.duration_min and 0 < s.duration_min <= 600 else None
    if duration and duration not in _ints(s.evidence):
        duration = None
    price = _clean(s.price)
    if price and not _grounded(price, s.evidence):
        price = None
    description = _clean(s.description)
    if description and not _grounded(description, s.evidence):
        description = None
    requirements = [r.strip() for r in s.requirements if r.strip() and _grounded(r, s.evidence)]
    names = [
        n.strip()
        for n in s.professional_names
        if n.strip() and any(t in evidence_fold for t in _name_tokens(n))
    ]
    everyone = s.all_professionals and ("todos" in evidence_fold or "todas" in evidence_fold)
    return s.model_copy(
        update={
            "name": s.name.strip(),
            "duration_min": duration,
            "price": price,
            "description": description,
            "requirements": requirements,
            "professional_names": names,
            "all_professionals": everyone,
        }
    )


def verify_plan(plan: ExtractedInsurancePlan, source_norm: str) -> ExtractedInsurancePlan | None:
    if not _supported(plan.evidence, source_norm):
        return None
    tokens = _significant(plan.name) or [fold(plan.name)]
    if not any(t in fold(plan.evidence) for t in tokens):
        return None
    note = _clean(plan.how_it_works)
    if note and not _grounded(note, plan.evidence):
        note = None
    return plan.model_copy(update={"name": plan.name.strip(), "how_it_works": note})


def clean_model_notes(notes: list[str]) -> list[str]:
    out: list[str] = []
    for text in notes:
        text = re.sub(r"\s+", " ", text or "").strip()
        if text and text not in out:
            out.append(text[:MAX_NOTE_CHARS])
    return out[:MAX_MODEL_NOTES]


# --------------------------------------------------------------------------- the proposal


@dataclass(frozen=True)
class ProposedProfessional:
    name: str
    matched_id: str | None  # the clinic's professional this is, or None = to be registered
    matched_name: str | None
    specialty: str | None
    specialty_status: str  # "new" | "same" | "different" | "none"
    about: str | None
    about_status: str
    # Shown to the manager as a suggestion for the invite. Never stored, never mailed to.
    email: str | None
    hours: Week | None
    hours_status: str
    evidence: str
    hours_evidence: str | None


@dataclass(frozen=True)
class ProposedCatalogService:
    name: str
    matched_id: str | None  # the clinic's catalog row, or None = to be created
    matched_name: str | None
    duration_min: int
    duration_assumed: bool
    price: str | None
    description: str | None
    requirements: list[str]
    # Names as they are in this proposal (or the clinic's roster). Empty + everyone=False means
    # the source did not say who does it.
    professionals: list[str]
    everyone: bool
    evidence: str


@dataclass(frozen=True)
class ProposedPlan:
    name: str
    catalog_id: str | None
    catalog_name: str | None
    loose: bool  # matched by a shorter spelling ("Bradesco" -> "Bradesco Saúde")
    custom_note: str | None  # a plan outside the catalog: how the clinic says it works
    status: str  # "new" | "same"
    evidence: str


@dataclass(frozen=True)
class ProposedInsurance:
    current_mode: str | None
    # No mode chosen yet: the screen sets the most common one ("shared": every doctor takes every
    # plan the clinic lists), because the source never says who takes what.
    default_mode: str | None
    applicable: bool
    plans: list[ProposedPlan]


@dataclass(frozen=True)
class ProposedNote:
    kind: str  # "decision" (the AI chose) | "gap" (the AI could not fill)
    text: str
    origin: str = "rule"  # "rule" (deterministic) | "model" (the model's own account)


@dataclass(frozen=True)
class CatalogProposal:
    professionals: list[ProposedProfessional] = field(default_factory=list)
    catalog_services: list[ProposedCatalogService] = field(default_factory=list)
    insurance: ProposedInsurance | None = None
    notes: list[ProposedNote] = field(default_factory=list)


def _status(new: Any, current: Any) -> str:
    if new in (None, "", [], {}):
        return "none"
    if current in (None, "", [], {}):
        return "new"
    return "same" if fold(str(new)) == fold(str(current)) else "different"


def _join(names: list[str]) -> str:
    quoted = [f"“{n}”" for n in names]
    if len(quoted) <= 1:
        return "".join(quoted)
    return ", ".join(quoted[:-1]) + " e " + quoted[-1]


def build_catalog_proposal(
    *,
    professionals: list[ExtractedProfessional],
    services: list[ExtractedService],
    plans: list[ExtractedInsurancePlan],
    model_notes: list[str],
    roster: list[Any],  # clinic professionals: id, name, specialty, about, business_hours
    catalog: list[Any],  # clinic service rows: id, name
    insurance_catalog: list[Any],  # global convênios: id, name, aliases
    clinic_plan_names: list[str],  # plans the clinic already has (catalog or "Outro"), by name
    insurance_mode: str | None,
    default_duration: int,
) -> CatalogProposal:
    notes: list[ProposedNote] = []

    # ---- professionals
    proposed: list[ProposedProfessional] = []
    seen: set[frozenset[str]] = set()
    roster_names = [str(r.name) for r in roster]
    for p in professionals[:MAX_PROFESSIONALS]:
        key = _name_tokens(p.name)
        if not key or key in seen:
            continue
        seen.add(key)
        index = match_name(p.name, roster_names)
        row = roster[index] if index is not None else None
        hours = week_from_groups(p.hours) or None
        proposed.append(
            ProposedProfessional(
                name=p.name,
                matched_id=str(row.id) if row else None,
                matched_name=str(row.name) if row else None,
                specialty=p.specialty,
                specialty_status=_status(p.specialty, getattr(row, "specialty", None)),
                about=p.about,
                about_status=_status(p.about, getattr(row, "about", None)),
                email=p.email,
                hours=hours,
                hours_status=_status(hours, getattr(row, "business_hours", None)),
                evidence=p.evidence,
                hours_evidence=p.hours_evidence if hours else None,
            )
        )
    proposed_names = [p.name for p in proposed]
    known_names = proposed_names + [
        n for n in roster_names if match_name(n, proposed_names) is None
    ]

    # ---- services
    out_services: list[ProposedCatalogService] = []
    seen_services: set[str] = set()
    unresolved: list[str] = []
    for s in services[:MAX_SERVICES]:
        ident = normalize_service(s.name)
        if not ident or ident in seen_services:
            continue
        seen_services.add(ident)
        row = next((c for c in catalog if normalize_service(str(c.name)) == ident), None)
        linked: list[str] = []
        for candidate in s.professional_names:
            at = match_name(candidate, known_names)
            if at is None:
                unresolved.append(candidate)
            elif known_names[at] not in linked:
                linked.append(known_names[at])
        out_services.append(
            ProposedCatalogService(
                name=s.name,
                matched_id=str(row.id) if row else None,
                matched_name=str(row.name) if row else None,
                duration_min=s.duration_min or default_duration,
                duration_assumed=s.duration_min is None,
                price=s.price[:MAX_PRICE_CHARS] if s.price else None,
                description=s.description,
                requirements=s.requirements,
                professionals=linked,
                everyone=s.all_professionals,
                evidence=s.evidence,
            )
        )

    # ---- convênios
    insurance = _build_insurance(plans, insurance_catalog, clinic_plan_names, insurance_mode, notes)

    # ---- what the rules decided on the clinic's behalf
    for service in out_services:
        if len(service.professionals) > 1:
            terms = " · ".join(t for t in (service.price, f"{service.duration_min} min") if t)
            notes.append(
                ProposedNote(
                    "decision",
                    f"{_join([service.name])} é feito por {_join(service.professionals)}, mas o "
                    f"texto dá um só valor e uma só duração ({terms}). Repeti o mesmo para todos.",
                )
            )
        if service.everyone:
            notes.append(
                ProposedNote(
                    "decision",
                    f"{_join([service.name])} vale para todos os profissionais, como o texto diz. "
                    "Ofereci a todos.",
                )
            )
    assumed = [s.name for s in out_services if s.duration_assumed]
    if assumed:
        notes.append(
            ProposedNote(
                "decision",
                f"O texto não diz a duração de {_join(assumed)}. Usei a duração padrão da "
                f"clínica ({default_duration} min); ajuste se for diferente.",
            )
        )
    if unresolved:
        notes.append(
            ProposedNote(
                "gap",
                f"O texto cita {_join(list(dict.fromkeys(unresolved)))} em algum serviço, mas não "
                "consegui ligar a nenhum profissional da lista. Esse vínculo ficou de fora.",
            )
        )
    no_prof = [s.name for s in out_services if not s.professionals and not s.everyone]
    if no_prof:
        notes.append(
            ProposedNote(
                "gap",
                f"O texto não diz quem faz {_join(no_prof)}. Entra no catálogo, mas ninguém o "
                "oferece ainda.",
            )
        )
    no_price = [s.name for s in out_services if not s.price]
    if no_price:
        notes.append(ProposedNote("gap", f"Sem valor no texto: {_join(no_price)}."))
    no_hours = [p.name for p in proposed if not p.hours and p.hours_status != "same"]
    if no_hours:
        notes.append(
            ProposedNote(
                "gap", f"Não encontrei dias e horários de atendimento de {_join(no_hours)}."
            )
        )
    mailed = [p.name for p in proposed if p.email]
    if mailed:
        notes.append(
            ProposedNote(
                "decision",
                f"O texto traz e-mail de {_join(mailed)}. Não enviei convite a ninguém: quando "
                "quiser que entrem no sistema, convide em “Equipe”.",
            )
        )
    notes.extend(
        ProposedNote("decision", t, origin="model") for t in clean_model_notes(model_notes)
    )

    return CatalogProposal(
        professionals=proposed,
        catalog_services=out_services,
        insurance=insurance,
        notes=notes,
    )


def _build_insurance(
    plans: list[ExtractedInsurancePlan],
    insurance_catalog: list[Any],
    clinic_plan_names: list[str],
    mode: str | None,
    notes: list[ProposedNote],
) -> ProposedInsurance | None:
    if not plans:
        return None
    have = {fold(n) for n in clinic_plan_names}
    keys: list[tuple[Any, set[str]]] = []
    for entry in insurance_catalog:
        spellings = {
            fold(entry.name),
            *(fold(str(a)) for a in (getattr(entry, "aliases", None) or [])),
        }
        keys.append((entry, {s for s in spellings if s}))

    def words(text: str) -> frozenset[str]:
        return frozenset(re.findall(r"[a-z0-9]+", text))

    out: list[ProposedPlan] = []
    loose: list[tuple[str, str]] = []
    custom: list[str] = []
    for plan in plans[:MAX_PLANS]:
        wanted = fold(plan.name)
        if not wanted or any(fold(p.name) == wanted for p in out):
            continue
        exact = [e for e, spell in keys if wanted in spell]
        partial = [
            e for e, spell in keys if words(wanted) and any(words(wanted) < words(s) for s in spell)
        ]
        entry, is_loose = None, False
        if len(exact) == 1:
            entry = exact[0]
        elif not exact and len(partial) == 1:
            entry, is_loose = partial[0], True
        if entry is not None:
            if is_loose:
                loose.append((plan.name, str(entry.name)))
            out.append(
                ProposedPlan(
                    name=str(entry.name),
                    catalog_id=str(entry.id),
                    catalog_name=str(entry.name),
                    loose=is_loose,
                    custom_note=None,
                    status="same" if fold(entry.name) in have else "new",
                    evidence=plan.evidence,
                )
            )
        else:
            custom.append(plan.name)
            out.append(
                ProposedPlan(
                    name=plan.name,
                    catalog_id=None,
                    catalog_name=None,
                    loose=False,
                    custom_note=(plan.how_it_works or plan.evidence)[:2000],
                    status="same" if wanted in have else "new",
                    evidence=plan.evidence,
                )
            )
    applicable = mode != "independent"
    default_mode = "shared" if mode is None else None
    if not applicable:
        notes.append(
            ProposedNote(
                "gap",
                "O texto lista convênios, mas esta clínica configurou convênio por profissional. "
                "Não mexi nisso: confira em “Convênios”.",
            )
        )
        return ProposedInsurance(mode, None, False, out)
    if default_mode:
        notes.append(
            ProposedNote(
                "decision",
                "O texto diz quais convênios a clínica aceita, mas não diz quais profissionais "
                "atendem cada um. Considerei que todos os profissionais aceitam todos os "
                "convênios; ajuste em “Convênios” se não for assim.",
            )
        )
    if loose:
        pairs = ", ".join(f"“{a}” como “{b}”" for a, b in loose)
        notes.append(
            ProposedNote("decision", f"Entendi {pairs} (nomes da nossa lista de convênios).")
        )
    if custom:
        notes.append(
            ProposedNote(
                "decision",
                f"{_join(custom)} não está na nossa lista de convênios. Cadastrei como “Outro”, "
                "com a explicação que o texto dá.",
            )
        )
    return ProposedInsurance(mode, default_mode, True, out)
