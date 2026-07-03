"""Terminal agent loop for SecretarIA (Fase A smoke test).

Uses the SAME LangGraph agent (build_agent) that the arq worker uses in
Fase B, just with in-memory history instead of the database. Lets you
exercise the agent end-to-end without WhatsApp/arq/postgres.

Pre-reqs in .env:
  - OPENAI_API_KEY, OPENAI_SECRETARIA_MODEL, OPENAI_MAX_TOKENS
  - GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN

Run:
    uv run python scripts/test_agent.py

Type 'sair' to leave.
"""

import asyncio

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from secretaria.ai.graph import build_agent
from secretaria.config import get_settings


def _print_tool_calls(new_messages: list[BaseMessage]) -> None:
    """Surface tool invocations so the dev can see what the LLM decided."""
    for msg in new_messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                print(f"   [tool] {tc['name']}({tc['args']})")


async def main() -> None:
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise SystemExit("OPENAI_API_KEY missing in .env")

    agent = build_agent()
    history: list[BaseMessage] = []

    print("=" * 60)
    print("SecretarIA - terminal agent (Fase A)")
    print(f"  model:    {settings.OPENAI_SECRETARIA_MODEL}")
    print(f"  calendar: {settings.GOOGLE_CALENDAR_ID}")
    print(f"  tz:       {settings.CLINIC_TIMEZONE}")
    print("Digite 'sair' para encerrar.")
    print("=" * 60)

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
        history_len = len(history)
        try:
            result = await agent.ainvoke({"messages": history})
        except Exception as exc:
            print(f"\n[erro] {exc}\n")
            history.pop()  # roll back the orphan user turn
            continue

        new_messages = result["messages"][history_len:]
        _print_tool_calls(new_messages)

        final = result["messages"][-1]
        text = (getattr(final, "content", "") or "").strip()
        if not text:
            print(
                "\n[!] resposta vazia — provavelmente OPENAI_MAX_TOKENS "
                "curto demais para o turno (gpt-5/o-series gastam reasoning).\n"
            )
            history.pop()
            continue

        history.append(AIMessage(content=text))
        print(f"\n👁️  {text}\n")


if __name__ == "__main__":
    asyncio.run(main())
