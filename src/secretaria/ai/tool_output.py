"""What a v2 tool may hand the model: a declared allowlist of output keys (TASK-030 P4).

The owner's rule (2026-10-03): on the v2 toolset the AI never sees a name or any other
patient's data - not pseudonymized, not in any form. The PRIMARY defense is that no v2 tool
reads such data in the first place: `get_availability` computes free windows and never
touches an event, `create_event`/`cancel_event` (ai/staging_tools.py) only stage the
patient's confirmation card, `list_patient_appointments` reads only this patient's rows.

This module is the structural wall behind that. Every tool of a v2 turn declares HERE which
keys its answer may carry (and, for a list of records, which keys each record may carry).
`wrap_tools_with_output_allowlist` - applied by ai/graph.py::build_agent on v2 turns only -
drops every other key before the answer re-enters the model's context and logs the dropped
key NAMES (never a value; a key that is not a plain identifier is logged as "<other>"). A
tool with no declaration answers an error instead of its data, so nothing - a Google API
dict included - is ever passed through by default. tests/test_ai_tool_output_allowlist.py
fails when a v2 tool has no declaration; tests/test_ai_v2_blindness.py runs every v2 tool
against decoy data and fails on any undeclared key.

The pseudonymization guard (ai/pii.py) stays as a SECOND wall, wrapped outside this one. It
is not the primary defense: it masks patterns (phone, CPF, e-mail) and values already in
the conversation's map, and - its own docstring says so - cannot mask an unknown third
party's name ("Consulta - Maria Silva" goes through it as itself).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from secretaria.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class OutputAllowlist:
    """The answer shape one tool may return to the model.

    `keys`: the top-level keys allowed. `records`: for a key whose value is a list of
    records, the keys each record may carry. `text`: the tool answers with one of its own
    sentences (a str) instead of a dict.
    """

    keys: frozenset[str]
    records: Mapping[str, frozenset[str]] = field(default_factory=dict)
    text: bool = False


_ERROR_ONLY = OutputAllowlist(frozenset({"error"}))

# Every tool a v2 turn can be built with, by model-facing NAME. A tool that only hands back
# to the flow (it raises; the model never reads a result) or refuses with an error declares
# just "error". Adding a tool to the v2 set without a line here fails
# tests/test_ai_tool_output_allowlist.py and, at runtime, makes the tool answer an error.
V2_TOOL_OUTPUTS: Mapping[str, OutputAllowlist] = {
    "get_availability": OutputAllowlist(
        frozenset(
            {
                "windows",
                "timezone",
                "slot_minutes",
                "day_from",
                "day_to",
                "professional",
                "clamped",
                "note",
                "error",
            }
        ),
        records={"windows": frozenset({"day", "start", "end"})},
    ),
    "list_patient_appointments": OutputAllowlist(
        frozenset({"appointments", "count", "nota", "error"}),
        records={"appointments": frozenset({"quando", "tipo"})},
    ),
    "list_professionals": OutputAllowlist(
        frozenset({"professionals", "error"}),
        records={"professionals": frozenset({"name", "specialty", "about"})},
    ),
    "list_units": OutputAllowlist(
        frozenset({"units", "error"}),
        records={"units": frozenset({"name", "address"})},
    ),
    "get_service_info": OutputAllowlist(
        frozenset(
            {
                "servico",
                "duracao_min",
                "preco",
                "descricao",
                "descricao_completa",
                "orientacoes",
                "error",
            }
        )
    ),
    "iniciar_pre_consulta": OutputAllowlist(frozenset({"error"}), text=True),
    "create_event": _ERROR_ONLY,
    "cancel_event": _ERROR_ONLY,
    "set_booking_draft": _ERROR_ONLY,
    "manage_existing_appointment": _ERROR_ONLY,
    "request_human_handoff": _ERROR_ONLY,
    "offer_human_handoff": _ERROR_ONLY,
    "show_main_menu": _ERROR_ONLY,
    "start_guided_booking": _ERROR_ONLY,
    "select_professional_and_continue": _ERROR_ONLY,
}

_SCALARS = (str, int, float, bool, type(None))
_LOGGABLE_KEY = re.compile(r"[a-z_][a-z0-9_]{0,40}")

UNDECLARED_ERROR = (
    "Esta ferramenta não está liberada para responder nesta clínica. Não invente o "
    "resultado: siga com o paciente pelo fluxo (show_main_menu)."
)
UNUSABLE_ERROR = (
    "A ferramenta não devolveu uma resposta utilizável. Não invente o resultado: tente de "
    "novo uma vez ou siga com o paciente pelo fluxo (show_main_menu)."
)


def _log_name(key: Any) -> str:
    """A key as it may appear in a log line: a plain identifier, else "<other>"."""
    text = str(key)
    return text if _LOGGABLE_KEY.fullmatch(text) else "<other>"


def _clean_records(key: str, value: Any, allowed: frozenset[str], dropped: list[str]) -> list:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        dropped.append(key)
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, Mapping):
            dropped.append(f"{key}[]")
            continue
        record: dict = {}
        for name, field_value in item.items():
            if name in allowed and isinstance(field_value, _SCALARS):
                record[name] = field_value
            else:
                dropped.append(f"{key}[].{_log_name(name)}")
        out.append(record)
    return out


def _clean_plain(key: str, value: Any, dropped: list[str]) -> tuple[bool, Any]:
    """A declared key that is not a record list: scalars or a list of scalars only."""
    if isinstance(value, _SCALARS):
        return True, value
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and all(isinstance(item, _SCALARS) for item in value)
    ):
        return True, list(value)
    dropped.append(key)
    return False, None


def undeclared_keys(tool_name: str, result: Any) -> list[str]:
    """The key NAMES in `result` its declaration does not allow ([] = clean).

    What the static/decoy tests assert on the RAW output of a tool, before any wrapper.
    """
    spec = V2_TOOL_OUTPUTS.get(tool_name)
    if spec is None:
        return ["<undeclared tool>"]
    if isinstance(result, str):
        return [] if spec.text else ["<text>"]
    if not isinstance(result, Mapping):
        return [f"<{type(result).__name__}>"]
    dropped: list[str] = []
    for key, value in result.items():
        if key not in spec.keys:
            dropped.append(_log_name(key))
        elif key in spec.records:
            _clean_records(key, value, spec.records[key], dropped)
        else:
            _clean_plain(key, value, dropped)
    return dropped


def allowlisted(tool_name: str, result: Any) -> Any:
    """`result` reduced to what `tool_name` declared; an error dict when nothing can pass."""
    spec = V2_TOOL_OUTPUTS.get(tool_name)
    if spec is None:
        logger.warning("agent_tool_output_undeclared", tool=tool_name)
        return {"error": UNDECLARED_ERROR}
    if isinstance(result, str):
        if spec.text:
            return result
        logger.warning("agent_tool_output_dropped", tool=tool_name, keys=["<text>"])
        return {"error": UNUSABLE_ERROR}
    if not isinstance(result, Mapping):
        logger.warning(
            "agent_tool_output_dropped", tool=tool_name, keys=[f"<{type(result).__name__}>"]
        )
        return {"error": UNUSABLE_ERROR}
    dropped: list[str] = []
    out: dict = {}
    for key, value in result.items():
        if key not in spec.keys:
            dropped.append(_log_name(key))
        elif key in spec.records:
            out[key] = _clean_records(key, value, spec.records[key], dropped)
        else:
            kept, cleaned = _clean_plain(key, value, dropped)
            if kept:
                out[key] = cleaned
    if dropped:
        logger.warning("agent_tool_output_dropped", tool=tool_name, keys=sorted(set(dropped)))
    if not out:
        return {"error": UNUSABLE_ERROR}
    return out


def _refusing(tool: Any) -> Any:
    name = getattr(tool, "name", str(tool))

    async def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        logger.warning("agent_tool_output_undeclared", tool=name)
        return {"error": UNDECLARED_ERROR}

    return tool.model_copy(update={"coroutine": _refuse})


def _wrap_one(tool: Any) -> Any:
    name = getattr(tool, "name", str(tool))
    inner = getattr(tool, "coroutine", None)
    if inner is None or name not in V2_TOOL_OUTPUTS:
        # Fail closed: a sync tool would bypass the filter, an undeclared one has no shape.
        return _refusing(tool)

    async def _allowlisted(*args: Any, **kwargs: Any) -> Any:
        # NOT wrapped in try/except: the hand-back exceptions (BookingDraftRequested,
        # ManageAppointmentRequested, ...) and CalendarUnavailableError must propagate
        # out of the ToolNode for run_agent to catch by type (same rule as ai/pii.py).
        return allowlisted(name, await inner(*args, **kwargs))

    # model_copy keeps name, description, args_schema and metadata: the model sees the same
    # tool and graph._tool_cache_key the same identity.
    return tool.model_copy(update={"coroutine": _allowlisted})


def wrap_tools_with_output_allowlist(tools: Sequence[Any]) -> tuple[Any, ...]:
    """Wrap a v2 turn's tools so each answer passes its declared allowlist (innermost)."""
    return tuple(_wrap_one(t) for t in tools)
