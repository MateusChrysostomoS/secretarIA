"""Ratchet: neutral worker code must not branch on the channel (TASK-024).

Scans `workers/shared/**`, `turn_router.py` and `orchestrator.py` for comparisons that involve a
`CHANNEL_*` constant or a `.channel` attribute, and requires the count per (file, function) to
EQUAL `ALLOWED`. The list only ever shrinks: remove an entry in the same commit that removes
its branch (the test also fails when an entry is stale, so progress cannot be forgotten).
The end state is the single entry for `_reply_sender`, which is the one deliberate place
that decides the sender of a channel (skill `channel-aware-dispatch`).

To branch on a channel anyway: add a field to `ChannelPolicy` instead.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import secretaria.workers as workers_pkg

ROOT = Path(workers_pkg.__file__).parent

# (path relative to workers/, function) -> number of channel comparisons.
# INITIAL inventory (main @ b9d5cd1). Delete entries as the stages remove the branches.
ALLOWED: dict[tuple[str, str], int] = {
    ("shared/greeting.py", "_asks_name_at_first_contact"): 1,
    ("shared/sender.py", "_reply_sender"): 1,
    ("turn_router.py", "_route_inbound_turn"): 1,
}


def _is_channel(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id.startswith("CHANNEL_")) or (
        isinstance(node, ast.Attribute) and node.attr == "channel"
    )


def _scan() -> Counter[tuple[str, str]]:
    found: Counter[tuple[str, str]] = Counter()
    files = [
        *sorted((ROOT / "shared").glob("*.py")),
        ROOT / "turn_router.py",
        ROOT / "orchestrator.py",
    ]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parent: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and any(
                _is_channel(x) for x in [node.left, *node.comparators]
            ):
                owner, func = node, "<module>"
                while owner in parent:
                    owner = parent[owner]
                    if isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
                        func = owner.name
                        break
                found[(path.relative_to(ROOT).as_posix(), func)] += 1
    return found


def test_no_new_channel_branches_in_neutral_code() -> None:
    found = _scan()
    assert dict(found) == ALLOWED, (
        "Channel comparisons in neutral worker code changed.\n"
        f"  found  : {dict(found)}\n  allowed: {ALLOWED}\n"
        "Read the channel from `policy_for(channel)` (workers/shared/channel_policy.py) instead "
        "of comparing it; if you REMOVED a branch, delete its entry from ALLOWED."
    )
