"""Liveness probe and build identity.

`/health` is the LIVENESS probe and its response body is deliberately FROZEN:
an orchestrator or load balancer may be matching on it, and "is Postgres/Redis
actually reachable?" is a readiness question that belongs to its own endpoint.
Build identity therefore lives on a separate `/build` route instead of being
appended here — and `/ready` is left unclaimed for the real readiness check.
"""

from typing import Any

from fastapi import APIRouter, Request

from secretaria.core.build_info import build_identity, compare_build, read_build_identity

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Intentionally does not touch Postgres or Redis."""
    return {"status": "ok"}


@router.get("/build")
async def build(request: Request) -> dict[str, Any]:
    """Which code is running here, and does the worker agree? (FIX_01 §5.1/§5.2)

    Answers "are the API and the worker on the same commit?" in ONE request,
    without opening the deploy panel's Environment: it reports this process's
    own identity plus the last identity the worker announced when it started,
    and the verdict comparing them.

    `worker: null` with `deploy_parity: "unknown"` means the worker has not
    announced itself — it is still running code from before this endpoint
    existed, or Redis is unreachable from here. It never means "in parity".

    Sanitised by construction: every field is build metadata (short commit sha,
    build timestamp, migration head, source hash). No environment value, DSN,
    host, token or job payload is read or returned — see core/build_info.py.
    """
    local = build_identity("api")
    # `app.state.arq_pool` is unset when the lifespan never ran (ASGITransport
    # in tests) and None when Redis was unreachable at startup; both degrade to
    # "worker unknown" rather than failing the request.
    pool = getattr(request.app.state, "arq_pool", None)
    peer = await read_build_identity(pool, "worker")
    return {
        **local.as_dict(),
        "worker": peer,
        "deploy_parity": compare_build(local, peer),
    }
