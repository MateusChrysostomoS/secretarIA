"""Guard (TASK-085 T4-g, SPEC §9): nobody reads a service's price/duration outside the resolver.

A service entry's price/duration can differ per professional x unit. The one place that knows how
to turn "entry + unit" into the value to show/charge/book is `services/service_terms.py`
(`resolve_entry_at_unit`, `resolve_terms`) and, on a router snapshot, `services/flow_units.py`
(`shown_terms`, `terms_at`). A reader that goes straight to `entry["price"]` /
`entry.get("duration_min")` / `.price` / `.duration_min` would silently show the BASE value to a
patient whose unit has an exception.

This test walks the AST of every `src/secretaria/**/*.py` (so comments and docstrings never count)
and finds those reads. Each one must live in a `(file, function)` listed in ALLOWLIST, with a
one-line reason. A second test fails when an allowlist entry no longer matches a real read, so the
list cannot rot. Writes (`x.price = ...`, `price=...` keyword arguments, `{"price": ...}` literals)
are not reads and are not flagged.

What counts as a read: `x.get("price"|"duration_min")`, `x["price"|"duration_min"]`,
`getattr(x, "price"|"duration_min"[, default])`, and a Load-context attribute `.price` /
`.duration_min`. "Message cost" style false positives (WhatsApp price, Pix amounts on deposit
rows) never use these exact names on an attribute/key, so the scanner does not see them.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "secretaria"
KEYS = frozenset({"price", "duration_min"})

INSTRUCTION = (
    "read price/duration through services/service_terms.py "
    "(resolve_entry_at_unit / flow_units.shown_terms|terms_at) "
    "or add an allowlist entry with a reason"
)


@dataclass(frozen=True)
class Read:
    path: str  # posix, relative to src/secretaria
    scope: str  # dotted qualified name of the innermost function/class, or <module>
    line: int
    kind: str


def scan_source(source: str, path: str) -> list[Read]:
    """Every price/duration read in `source` (AST-based), tagged with its enclosing scope."""
    tree = ast.parse(source)
    reads: list[Read] = []
    stack: list[str] = []

    def add(node: ast.AST, kind: str) -> None:
        reads.append(Read(path, ".".join(stack) or "<module>", node.lineno, kind))

    def visit(node: ast.AST) -> None:
        scoped = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        if scoped:
            stack.append(node.name)
        if isinstance(node, ast.Attribute) and node.attr in KEYS and isinstance(node.ctx, ast.Load):
            add(node, f".{node.attr}")
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in KEYS
        ):
            add(node, f'["{node.slice.value}"]')
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in KEYS
            ):
                add(node, f'.get("{node.args[0].value}")')
            elif (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in KEYS
            ):
                add(node, f'getattr(.., "{node.args[1].value}")')
        for child in ast.iter_child_nodes(node):
            visit(child)
        if scoped:
            stack.pop()

    visit(tree)
    return reads


def scan_tree(root: Path = SRC) -> list[Read]:
    out: list[Read] = []
    for file in sorted(root.rglob("*.py")):
        rel = file.relative_to(root).as_posix()
        out.extend(scan_source(file.read_text(encoding="utf-8"), rel))
    return out


# --- allowlist: (path, scope) -> reason. Groups are comments; the reason is the contract. ---------
ALLOWLIST: dict[tuple[str, str], str] = {
    # == The resolver itself ==================================================================
    ("services/service_terms.py", "_base_duration"): "resolver: the entry's base duration",
    ("services/service_terms.py", "resolve_terms"): "resolver: base + the unit's exception",
    ("services/service_terms.py", "price_differs_across_units"): "resolver: compares resolved",
    ("services/service_terms.py", "load_terms_for"): "resolver: loads the exception rows",
    ("services/flow_units.py", "shown_terms"): "D3/D7 rule on a snapshot, over resolve_terms",
    # == Readers switched in T4 (read the base only AFTER/ALONGSIDE resolving) ================
    ("ai/availability_tool.py", "_unit_minutes"): "reads ResolvedTerms of resolve_entry_at_unit",
    ("ai/prompts.py", "_format_appointment_types"): (
        "formats RuntimeAppointmentType, already D3-resolved by tenant_config"
    ),
    ("ai/scoped_help.py", "_service_price"): (
        "no exception -> base; else entry_shown_terms (D3 sentence, never a number)"
    ),
    ("ai/tools.py", "get_service_info"): (
        "RuntimeAppointmentType (resolved) first, then overridden by the chosen unit's terms"
    ),
    ("services/flow_router.py", "_service_duration_at"): (
        "flow_units.terms_at; falls back to _service_duration (base) when no own value"
    ),
    ("services/flow_router.py", "_priced_entry"): (
        "flow_units.shown_terms; compares to base only to keep the same dict when equal"
    ),
    ("services/flow_router.py", "_with_unit_terms_line"): "flow_units.shown_terms(...).price",
    ("services/payments/deposit_lifecycle.py", "_price_at_unit"): (
        "base for the no-exception case; the unit's own price via resolve_entry_at_unit"
    ),
    ("services/payments/readiness.py", "_unit_prices"): (
        "D8: base line + one line per bookable unit with its own price (terms_for_entry)"
    ),
    ("services/payments/readiness.py", "_service_prices"): (
        "base line of each entry; the per-unit lines come from _unit_prices"
    ),
    ("services/staff_appointment_edit.py", "_resolve_target"): (
        "D6: resolve_entry_at_unit's own duration, else the service base as before"
    ),
    ("services/staff_appointment_edit.py", "_own_price"): "reads ResolvedTerms vs the base price",
    ("services/staff_appointment_edit.py", "_unit_reprices"): (
        "compares ResolvedTerms of resolve_entry_at_unit (old unit vs new unit)"
    ),
    ("services/tenant_config.py", "_runtime_appointment_type"): (
        "load_tenant_config bottleneck: base, then entry_shown_terms (D3) overrides"
    ),
    ("workers/shared/greeting.py", "_load_upcoming_greeting_data"): (
        "ResolvedTerms of resolve_entry_at_unit at the booking's unit (D6/R11)"
    ),
    ("workers/shared/llm_context.py", "_nearest_price"): (
        "runtime price when no exceptions; else flow_units.terms_at at the booking's unit"
    ),
    # == Class (c): not a reader of the price/duration to show (inventory 1.3) ================
    ("ai/availability_tool.py", "_entry_minutes"): (
        "calendar/window length helper over a catalog entry (class c, no price shown)"
    ),
    ("services/flow_router.py", "_service_duration"): (
        "base duration, the no-unit value (D7) and the fallback of _service_duration_at"
    ),
    ("services/tenant_config.py", "resolve_professional_calendar"): (
        "calendar default slot length = first active service's base (class c)"
    ),
    ("services/booking_details.py", "service_price_text"): (
        "pure formatter: callers pass an entry already priced by _priced_entry"
    ),
    ("services/context_catalog_proposal.py", "verify_service"): (
        "catalog proposal read from the clinic's own text, not a booking (class c)"
    ),
    ("services/context_catalog_proposal.py", "build_catalog_proposal"): (
        "catalog proposal read from the clinic's own text, not a booking (class c)"
    ),
    ("api/hub/services.py", "create_service"): "hub write: the offer's BASE goes to the entry",
    ("services/hub_unit_terms.py", "apply_unit_terms"): "hub write/validation of the exceptions",
    ("services/hub_unit_terms.py", "entries_with_terms"): "hub read model: base + own rows",
    ("schemas/professional.py", "UnitTermsItem._something_to_override"): (
        "hub schema validation (an exception must override something)"
    ),
}


def _allowed(read: Read) -> bool:
    return (read.path, read.scope) in ALLOWLIST


def test_every_price_or_duration_read_goes_through_the_resolver_or_is_justified():
    offenders = [r for r in scan_tree() if not _allowed(r)]
    message = "\n".join(
        f"  src/secretaria/{r.path}:{r.line} in {r.scope}: {r.kind}" for r in offenders
    )
    assert not offenders, (
        f"service price/duration read outside the resolver:\n{message}\n{INSTRUCTION}"
    )


def test_every_allowlist_entry_still_matches_a_real_read():
    live = {(r.path, r.scope) for r in scan_tree()}
    stale = sorted(set(ALLOWLIST) - live)
    assert not stale, "allowlist entries that match no read any more (remove them): " + ", ".join(
        f"{p}::{s}" for p, s in stale
    )
    assert all(reason.strip() for reason in ALLOWLIST.values())


def test_the_scanner_sees_reads_and_ignores_comments_docstrings_and_writes():
    source = '''
"""price duration_min in a module docstring"""
# entry.get("price") in a comment
class Box:
    def reads(self, entry, row):
        """entry["price"] in a docstring"""
        a = entry.get("price")
        b = entry["duration_min"]
        c = row.price
        d = getattr(row, "duration_min", None)
        return a, b, c, d

    def writes(self, entry):
        entry.price = "R$ 1"
        entry["price"] = "R$ 1"
        return dict(price="R$ 1", duration_min=3), {"price": 1}
'''
    found = scan_source(source, "x.py")
    assert {(r.scope, r.kind) for r in found} == {
        ("Box.reads", '.get("price")'),
        ("Box.reads", '["duration_min"]'),
        ("Box.reads", ".price"),
        ("Box.reads", 'getattr(.., "duration_min")'),
    }


def test_a_new_reader_outside_the_allowlist_is_reported_with_file_and_line():
    source = "def show(entry):\n    return entry.get('price')\n"
    reads = scan_source(source, "services/new_reader.py")
    assert [(r.path, r.scope, r.line) for r in reads] == [("services/new_reader.py", "show", 2)]
    assert not _allowed(reads[0])
    assert "resolve_entry_at_unit" in INSTRUCTION
