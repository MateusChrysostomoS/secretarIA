"""Choose who is looking at the hub agenda in a test (TASK-044 R7).

tests/conftest.py makes every test clinic-wide by default (the pre-R7 behaviour);
`view_as` replaces that for the current test only - conftest pops it afterwards.
"""

from secretaria.services.agenda_visibility import AgendaViewer


def view_as(viewer: AgendaViewer) -> None:
    from secretaria.api.hub.deps import get_agenda_viewer
    from secretaria.main import app

    app.dependency_overrides[get_agenda_viewer] = lambda: viewer
