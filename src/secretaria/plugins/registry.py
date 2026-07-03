"""The plugin registry: id -> PluginSpec, filtered per tenant by entitlement.

Starts EMPTY. Later rounds call `register(spec)` (typically at import time of
the module defining the plugin) to add capabilities. Nothing in this file
knows about any specific plugin.
"""

from secretaria.core.logging import get_logger
from secretaria.plugins.base import InboundContext, PluginSpec, PostBookingContext
from secretaria.services.entitlements_client import EntitlementSummary, is_entitled

logger = get_logger(__name__)

REGISTRY: dict[str, PluginSpec] = {}


def register(spec: PluginSpec) -> None:
    """Add (or replace) a plugin in the registry, keyed by `spec.id`."""
    REGISTRY[spec.id] = spec


def enabled_plugins(summary: EntitlementSummary) -> list[PluginSpec]:
    """PluginSpecs this tenant is currently entitled to.

    A disabled plugin (tenant not entitled to ANY of its `entitlement_keys`)
    contributes nothing — it is simply absent from the returned list.
    """
    return [
        spec
        for spec in REGISTRY.values()
        if any(is_entitled(summary, key) for key in spec.entitlement_keys)
    ]


def agent_tools_for(summary: EntitlementSummary) -> list:
    """Flatten `agent_tools` across every plugin this tenant is entitled to."""
    tools: list = []
    for spec in enabled_plugins(summary):
        tools.extend(spec.agent_tools)
    return tools


async def run_on_inbound(summary: EntitlementSummary, ctx: InboundContext) -> bool:
    """Run `on_inbound` hooks for every plugin this tenant is entitled to.

    Stops at the first hook that returns True ("handled — suppress the
    normal bot reply"). Plugins without an `on_inbound` hook (the default)
    are skipped. Returns False when no entitled plugin has a hook, or none
    of them handled this turn.
    """
    for spec in enabled_plugins(summary):
        if spec.on_inbound is None:
            continue
        if await spec.on_inbound(ctx):
            return True
    return False


async def run_post_booking(summary: EntitlementSummary, ctx: PostBookingContext) -> None:
    """Run `post_booking` hooks for every plugin this tenant is entitled to.

    Unlike `run_on_inbound`, there is NO short-circuit: every entitled
    plugin with a `post_booking` hook runs, regardless of what an earlier
    hook did or whether it failed. Each hook call is wrapped in its own
    try/except HERE (not left to the hook implementation) so one failing
    hook — say, the `ehr` addon's push erroring — never stops a later one —
    say, `analytics_bi` — from running this same sweep.
    """
    for spec in enabled_plugins(summary):
        if spec.post_booking is None:
            continue
        try:
            await spec.post_booking(ctx)
        except Exception as exc:
            logger.warning(
                "post_booking_hook_failed",
                plugin_id=spec.id,
                error=str(exc),
                tenant_id=str(ctx.tenant.id),
            )
