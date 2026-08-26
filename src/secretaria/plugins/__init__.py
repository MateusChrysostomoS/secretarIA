"""Plugin foundation: optional per-tenant capabilities gated by entitlements.

A "plugin" is a `PluginSpec` (see `base.py`) registered in `registry.REGISTRY`.
Each spec is gated behind one or more entitlement keys (addon ids or a
subscription tier, see `services/entitlements_client.py`; ANY-OF semantics).
The pipeline (workers/tasks.py) filters the registry per tenant via
`registry.enabled_plugins`/`registry.is_entitled` and threads the resulting
LangChain tools into the agent (`registry.agent_tools_for`) and the
`on_inbound` hooks into `registry.run_on_inbound`. An empty registry — or a
tenant entitled to nothing extra — contributes nothing: the base agent is
unchanged.

Importing the concrete plugin modules here (rather than leaving the registry
empty until someone imports them directly) means every entry point that
touches `secretaria.plugins` — workers/tasks.py, workers/arq_worker.py's cron
wiring — sees the full registry without needing to know which plugin modules
exist.
"""

from secretaria.plugins import (  # noqa: F401
    analytics_bi,
    ehr,
    human_backup,
    multi_professional,
    multi_unit,
    pix_deposit,
    precheck_handoff,
    professional_notification,
    reminders,
)
