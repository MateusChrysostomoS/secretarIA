"""Patch a worker-level name in every module the split put it in (TASK-023).

Before the split, `monkeypatch.setattr(tasks, "async_session_factory", fake)` was enough:
all the worker code lived in `secretaria.workers.tasks` and looked the name up there.
After it, the same code is spread over several modules, each with its own binding of
the name. `workers_ns` is a stand-in for the old module: setting an attribute on it sets
it on EVERY split module that has that attribute (plus the `tasks` facade); reading one
reads the facade. `monkeypatch.setattr(workers_ns, "X", value)` therefore means exactly
what `monkeypatch.setattr(tasks, "X", value)` used to mean, and undo restores them all.

It is deliberately explicit (a name you import and pass), not an autouse fixture: a test
that patches the workers says so in its first argument.
"""

from __future__ import annotations

import sys
from types import ModuleType

_PACKAGES = (
    "secretaria.workers.shared.",
    "secretaria.workers.whatsapp.",
    "secretaria.workers.portal.",
)
_SINGLES = {
    "secretaria.workers.tasks",
    "secretaria.workers.turn_router",
    "secretaria.workers.orchestrator",
}


def _split_modules() -> list[ModuleType]:
    import secretaria.workers.tasks  # noqa: F401  (imports every split module)

    return [
        module
        for name, module in list(sys.modules.items())
        if module is not None and (name in _SINGLES or name.startswith(_PACKAGES))
    ]


class _WorkersNamespace:
    """Fan-out proxy. Per-module originals are remembered, because the value pytest's
    `monkeypatch` hands back on undo is the FACADE's original - writing that into every
    module would make modules that own distinct objects (each has its own `logger`)
    share one after the first test that patches it, and leak between tests."""

    def __init__(self) -> None:
        object.__setattr__(self, "_saved", {})  # name -> {module name: original value}

    def __getattr__(self, name: str):
        import secretaria.workers.tasks as facade

        return getattr(facade, name)

    def __setattr__(self, name: str, value) -> None:
        import secretaria.workers.tasks as facade

        saved = self._saved
        if name in saved and value is saved[name][facade.__name__]:
            # monkeypatch undoing its OUTERMOST patch: give every module its own original.
            for module in _split_modules():
                if module.__name__ in saved[name]:
                    setattr(module, name, saved[name][module.__name__])
            del saved[name]
            return
        owners = [m for m in _split_modules() if hasattr(m, name)]
        if not owners:
            raise AttributeError(f"no secretaria.workers module defines {name!r}")
        if name not in saved:
            saved[name] = {m.__name__: getattr(m, name) for m in owners}
        for module in owners:
            setattr(module, name, value)

    def __delattr__(self, name: str) -> None:
        for module in _split_modules():
            if hasattr(module, name):
                delattr(module, name)


workers_ns = _WorkersNamespace()
