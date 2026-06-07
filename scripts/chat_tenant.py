"""Terminal chat with the agent DRESSED by a real tenant's hub config.

Unlike scripts/test_agent.py (which renders the env-var fallback prompt), this
loads the tenant row + load_tenant_config and threads it through the SAME
ContextVars that workers/tasks.py -> run_agent uses. So the greeting, persona,
appointment types, business hours and per-tenant Calendar credentials all take
effect — this is how you verify that editing /tenants/me/config actually
changes the conversation, without going through WhatsApp.

History is kept in memory (this script does not read/write the messages table).

Usage:
    uv run python scripts/chat_tenant.py
    uv run python scripts/chat_tenant.py <tenant_id>
"""

import asyncio
import sys
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy import select

from secretaria.ai.graph import _tenant_config_ctx, invoke_agent
from secretaria.ai.tools import _calendar_ctx
from secretaria.config import get_settings
from secretaria.core.database import async_session_factory, engine
from secretaria.models import Tenant
from secretaria.services.calendar import CalendarService
from secretaria.services.tenant_config import TenantRuntimeConfig, load_tenant_config


async def _load_config(wanted_id: str | None) -> TenantRuntimeConfig:
    async with async_session_factory() as session:
        if wanted_id:
            tenant = await session.get(Tenant, UUID(wanted_id))
        else:
            rows = (await session.scalars(select(Tenant).limit(2))).all()
            tenant = rows[0] if len(rows) == 1 else None
        if tenant is None:
            raise SystemExit(
                "Could not resolve a single tenant. Seed one with "
                "scripts/seed_dev.py, or pass a tenant_id."
            )
        return await load_tenant_config(session, tenant)


async def main() -> None:
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise SystemExit("OPENAI_API_KEY missing in .env")

    config = await _load_config(sys.argv[1] if len(sys.argv) > 1 else None)

    # Dress the agent exactly like run_agent does: per-tenant Calendar +
    # per-tenant prompt, injected via ContextVars.
    cal = (
        CalendarService.from_tenant_config(config)
        if config.google_refresh_token
        else CalendarService()
    )
    _calendar_ctx.set(cal)
    _tenant_config_ctx.set(config)

    print("=" * 60)
    print(f"Conversando com a secretarIA da: {config.clinic_name}")
    print(f"  idioma:   {config.language}    tz: {config.timezone}")
    print(f"  motivos:  {[t.name for t in config.appointment_types] or 'nenhum configurado'}")
    print(f"  calendar: {'tenant (tenant_credentials)' if config.google_refresh_token else 'env fallback (GOOGLE_REFRESH_TOKEN)'}")
    print("Digite 'sair' para encerrar.")
    print("=" * 60)

    history: list[BaseMessage] = []
    while True:
        try:
            user = input("\nVocê> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user.lower() in {"sair", "exit", "quit"}:
            break

        history.append(HumanMessage(content=user))
        try:
            text = await invoke_agent(history)
        except Exception as exc:
            print(f"\n[erro] {exc}\n")
            history.pop()
            continue
        if not text:
            print("\n[!] resposta vazia (OPENAI_MAX_TOKENS curto demais?)\n")
            history.pop()
            continue
        history.append(AIMessage(content=text))
        print(f"\n👁️  {text}\n")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
