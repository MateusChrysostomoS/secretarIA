"""LangGraph orchestration - STUB.

Placeholder for the conversational AI graph. The real graph (intent
detection -> routing -> tool calls -> response synthesis) is a future phase.
"""

from secretaria.core.logging import get_logger

logger = get_logger(__name__)

# TODO(ai): build the real LangGraph graph here. Planned shape:
#   1. intent node    - classify the patient's message.
#   2. router node    - schedule / reschedule / cancel / FAQ / precheck.
#   3. tool nodes     - calendar + precheck tools (see ai/tools.py).
#   4. response node  - synthesize the natural-language reply.
# Compile the graph once at import time and call `.ainvoke()` from run_agent.


async def run_agent(message: str, context: dict) -> str:
    """Run the conversational agent for a single inbound message.

    STUB: returns a fixed reply until the LangGraph graph is implemented.

    Args:
        message: the patient's message text.
        context: conversation context (ids, history, tenant info, ...).
    """
    logger.info("ai_run_agent_stub", message_preview=message[:80])
    # TODO(ai): replace with `return await graph.ainvoke(...)`.
    return "Recebi sua mensagem 🤖"
