"""Layer rules for the split of workers/tasks.py (TASK-023).

  shared/*     neutral: may NOT import whatsapp/* or portal/*
  whatsapp/*   may import shared/* and the composition layer; NOT portal/*
  portal/*     may import shared/* and the composition layer; NOT whatsapp/*
  turn_router, orchestrator
               composition layer: the only modules that may know both channels
  tasks        facade: re-exports only; nothing inside workers/ may import it

A violation means a channel detail leaked into neutral code (or the other way round),
which is what the folder split exists to prevent.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import secretaria.workers as workers_pkg

ROOT = Path(workers_pkg.__file__).parent
COMPOSITION = {"turn_router", "orchestrator"}
FORBIDDEN = {
    "shared": ("whatsapp", "portal", "tasks"),
    "whatsapp": ("portal", "tasks"),
    "portal": ("whatsapp", "tasks"),
}


def _imported_workers_modules(path: Path) -> set[str]:
    """`secretaria.workers.<first>` segments imported by a file."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        for name in names:
            parts = name.split(".")
            if parts[:2] == ["secretaria", "workers"] and len(parts) > 2:
                found.add(parts[2])
    return found


def _modules(package: str) -> list[Path]:
    return sorted(p for p in (ROOT / package).glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("package", sorted(FORBIDDEN))
def test_channel_packages_respect_the_layers(package: str) -> None:
    modules = _modules(package)
    assert modules, f"workers/{package}/ is empty - the split is missing"
    for path in modules:
        bad = _imported_workers_modules(path) & set(FORBIDDEN[package])
        assert not bad, f"workers/{package}/{path.name} imports {sorted(bad)}"


@pytest.mark.parametrize("name", sorted(COMPOSITION))
def test_composition_modules_never_import_the_facade(name: str) -> None:
    assert "tasks" not in _imported_workers_modules(ROOT / f"{name}.py")


def test_tasks_is_a_pure_facade() -> None:
    tree = ast.parse((ROOT / "tasks.py").read_text(encoding="utf-8"))
    code = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    ]
    assert not code, [n.name for n in code]


@pytest.mark.parametrize(
    "module",
    [
        "secretaria.workers.arq_worker",
        "secretaria.workers.tasks",
        "secretaria.workers.orchestrator",
        "secretaria.workers.turn_router",
        "secretaria.workers.whatsapp.inbound",
        "secretaria.workers.portal.inbound",
        "secretaria.workers.shared.sentinels",
        "secretaria.workers.shared.handback_log",
    ],
)
def test_each_entry_point_imports_alone(module: str) -> None:
    """A cycle only shows for some import orders; a fresh interpreter per entry point
    is the only honest check (in-process, an earlier test has already imported all)."""
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-c", f"import {module}"], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr[-800:]


def test_facade_still_exports_the_arq_jobs() -> None:
    from secretaria.workers import tasks

    for job in (
        "process_webhook_event",
        "process_brain_message_inbound",
        "process_brain_message_open",
        "process_message_statuses",
        "send_cancellation_notice",
        "send_patient_notification",
        "send_transactional_email",
        "transcribe_audio_message",
        "check_handover_timeouts",
    ):
        assert callable(getattr(tasks, job)), job
