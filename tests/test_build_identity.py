"""Build identity and the deploy-divergence alarm (FIX_01 §5.1/§5.2).

The failure these tests exist for: `secretaria_api` and `secretaria-worker` are
deployed by hand, separately, with no auto-deploy. On 2026-08-16 the worker was
left on an older commit and every greeting came from stale code while the API
looked healthy. Nothing in the system could answer "are these two on the same
commit?" — so the guarantees under test are (a) each process can prove what it
runs, (b) a mismatch alarms by itself, and (c) neither proof leaks a secret.
"""

import json

import pytest
from httpx import AsyncClient

from secretaria.config import get_settings
from secretaria.core import build_info
from secretaria.core.build_info import (
    UNKNOWN,
    BuildIdentity,
    build_identity,
    check_deploy_parity,
    compare_build,
    publish_build_identity,
    read_build_identity,
)
from secretaria.workers import arq_worker

# The full field set `/build` is allowed to return. A new key must be a
# deliberate edit here, not a silent addition to a response that is served
# unauthenticated.
BUILD_RESPONSE_FIELDS = {
    "service",
    "build_sha",
    "built_at",
    "alembic_head",
    "source_fingerprint",
    "worker",
    "deploy_parity",
}


class FakeRedis:
    """`set`/`get` in a dict — the only two calls build_info makes."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str) -> None:
        self.store[key] = value

    async def get(self, key: str) -> bytes | None:
        raw = self.store.get(key)
        # Bytes, like a real redis client with decode_responses off.
        return raw.encode() if raw is not None else None


class BrokenRedis:
    """Every call raises — the "Redis went away mid-flight" shape."""

    async def set(self, *_args: object) -> None:
        raise RuntimeError("redis down")

    async def get(self, *_args: object) -> None:
        raise RuntimeError("redis down")


class Recorder:
    """Captures structlog calls (house pattern, see test_appointment_status_taxonomy)."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def _record(self, event: str, **kwargs: object) -> None:
        self.records.append((event, kwargs))

    info = warning = error = debug = _record

    @property
    def events(self) -> list[str]:
        return [event for event, _ in self.records]

    def fields(self, event: str) -> dict:
        return next(fields for name, fields in self.records if name == event)


def make_identity(**overrides: str) -> BuildIdentity:
    base = {
        "service": "api",
        "build_sha": "39a472d",
        "built_at": "2026-08-16T11:20:00Z",
        "alembic_head": "c9a1e2f4b6d8",
        "source_fingerprint": "9f2c41ab77de",
    }
    return BuildIdentity(**{**base, **overrides})  # type: ignore[arg-type]


def configured_secrets() -> list[str]:
    """Values that must never appear in a build-identity payload or log line."""
    settings = get_settings()
    return [
        settings.ENCRYPTION_KEY,
        settings.META_APP_SECRET,
        settings.META_ACCESS_TOKEN,
        settings.META_VERIFY_TOKEN,
        settings.DATABASE_URL,
        settings.REDIS_URL,
    ]


# --------------------------------------------------------------------------
# Liveness contract (regression — FIX_01 §7)
# --------------------------------------------------------------------------


async def test_health_body_is_unchanged(client: AsyncClient) -> None:
    """`/health` is a frozen contract: a load balancer may match on this body.

    Build identity went to its own route precisely so this stays true.
    """
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --------------------------------------------------------------------------
# GET /build
# --------------------------------------------------------------------------


async def test_build_endpoint_proves_what_this_process_runs(client: AsyncClient) -> None:
    response = await client.get("/build")
    assert response.status_code == 200
    body = response.json()

    assert set(body) == BUILD_RESPONSE_FIELDS
    assert body["service"] == "api"
    # Resolved from the migration scripts on disk, not a placeholder — this is
    # the field that says which schema the deployed code expects.
    assert body["alembic_head"] != UNKNOWN
    assert len(body["source_fingerprint"]) == 12


async def test_build_endpoint_reports_unknown_not_parity_without_a_peer(
    client: AsyncClient,
) -> None:
    """A silent worker must never read as agreement.

    Under ASGITransport the lifespan never runs, so there is no arq pool — the
    same shape as an API whose Redis is unreachable.
    """
    body = (await client.get("/build")).json()
    assert body["worker"] is None
    assert body["deploy_parity"] == "unknown"


async def test_build_endpoint_exposes_no_secret(client: AsyncClient) -> None:
    """The endpoint that proves the deploy must not become the leak."""
    payload = (await client.get("/build")).text
    for secret in configured_secrets():
        assert secret, "test env must configure this to make the assertion meaningful"
        assert secret not in payload


# --------------------------------------------------------------------------
# Identity metadata
# --------------------------------------------------------------------------


def test_build_identity_reads_the_injected_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ARG`/`ENV` from the image reaches the identity, shortened."""
    monkeypatch.setenv("BUILD_SHA", "39a472d1c2b3a4e5f60718293a4b5c6d7e8f9a0b")
    monkeypatch.setenv("BUILT_AT", "2026-08-16T11:20:00Z")
    get_settings.cache_clear()
    build_identity.cache_clear()
    try:
        identity = build_identity("api")
        assert identity.build_sha == "39a472d"
        assert identity.built_at == "2026-08-16T11:20:00Z"
        assert identity.service == "api"
    finally:
        get_settings.cache_clear()
        build_identity.cache_clear()


def test_short_sha_normalises_both_spellings() -> None:
    """A full sha from a pipeline and a short one from a panel are the same commit.

    Without this, a correct deploy would alarm as a divergence.
    """
    assert build_info._short_sha("39a472d1c2b3a4e5f60718293a4b5c6d7e8f9a0b") == "39a472d"
    assert build_info._short_sha("39a472d") == "39a472d"
    # Deploy panels persist values with the quotes included (see cors_origins).
    assert build_info._short_sha("  '39a472d'  ") == "39a472d"
    assert build_info._short_sha("") == UNKNOWN
    # A non-sha build id is a legitimate label and must survive intact.
    assert build_info._short_sha("v1.4.2") == "v1.4.2"


def test_alembic_head_is_a_real_revision() -> None:
    assert build_info.alembic_head() != UNKNOWN
    # One head, or an explicit `a+b` when the migration tree has forked.
    assert build_info.alembic_head().strip()


# --------------------------------------------------------------------------
# The comparator (FIX_01 §7: "sinaliza divergência e silencia na paridade")
# --------------------------------------------------------------------------


def test_compare_build_flags_divergence() -> None:
    peer = make_identity(
        service="worker", build_sha="c55321d", source_fingerprint="1122aabbccdd"
    ).as_dict()
    assert compare_build(make_identity(), peer) == "divergent"


def test_compare_build_is_silent_on_parity() -> None:
    peer = make_identity(service="worker").as_dict()
    assert compare_build(make_identity(), peer) == "match"


def test_compare_build_falls_back_to_the_fingerprint() -> None:
    """The normal case today: nothing passes BUILD_SHA, parity still provable."""
    local = make_identity(build_sha=UNKNOWN)
    peer = make_identity(service="worker", build_sha=UNKNOWN).as_dict()
    assert compare_build(local, peer) == "match"


def test_compare_build_catches_a_lying_sha() -> None:
    """Same sha label, different code — any disagreement wins."""
    peer = make_identity(service="worker", source_fingerprint="1122aabbccdd").as_dict()
    assert compare_build(make_identity(), peer) == "divergent"


def test_compare_build_never_reports_unknown_as_parity() -> None:
    assert compare_build(make_identity(), None) == "unknown"
    assert compare_build(make_identity(), {}) == "unknown"
    blind = make_identity(build_sha=UNKNOWN, source_fingerprint=UNKNOWN)
    assert compare_build(blind, make_identity(service="worker").as_dict()) == "unknown"


# --------------------------------------------------------------------------
# The alarm
# --------------------------------------------------------------------------


async def test_deploy_parity_alarms_on_divergence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact 2026-08-16 shape: worker on an older commit than the API."""
    recorder = Recorder()
    monkeypatch.setattr(build_info, "logger", recorder)
    redis = FakeRedis()
    await publish_build_identity(
        redis,
        make_identity(service="worker", build_sha="c55321d", source_fingerprint="1122aabbccdd"),
    )

    verdict = await check_deploy_parity(redis, make_identity())

    assert verdict == "divergent"
    assert recorder.events == ["deploy_sha_divergence"]
    fields = recorder.fields("deploy_sha_divergence")
    assert fields["build_sha"] == "39a472d"
    assert fields["peer_service"] == "worker"
    assert fields["peer_build_sha"] == "c55321d"


async def test_deploy_parity_does_not_alarm_on_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    monkeypatch.setattr(build_info, "logger", recorder)
    redis = FakeRedis()
    await publish_build_identity(redis, make_identity(service="worker"))

    verdict = await check_deploy_parity(redis, make_identity())

    assert verdict == "match"
    assert recorder.events == ["deploy_sha_parity"]


async def test_deploy_parity_without_a_peer_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pool, no peer — reported as unknown, never as agreement."""
    recorder = Recorder()
    monkeypatch.setattr(build_info, "logger", recorder)

    assert await check_deploy_parity(None, make_identity()) == "unknown"
    assert recorder.events == ["deploy_sha_unknown"]


async def test_parity_log_carries_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    monkeypatch.setattr(build_info, "logger", recorder)
    redis = FakeRedis()
    await publish_build_identity(redis, make_identity(service="worker", build_sha="c55321d"))

    await check_deploy_parity(redis, make_identity())

    rendered = json.dumps(recorder.records, default=str)
    for secret in configured_secrets():
        assert secret not in rendered


# --------------------------------------------------------------------------
# Redis handling
# --------------------------------------------------------------------------


async def test_published_identity_roundtrips() -> None:
    redis = FakeRedis()
    identity = make_identity(service="worker")

    await publish_build_identity(redis, identity)
    stored = json.loads(redis.store["secretaria:build:worker"])
    assert set(stored) == set(identity.as_dict()) | {"reported_at"}

    peer = await read_build_identity(redis, "worker")
    assert peer is not None
    assert peer["build_sha"] == "39a472d"
    assert peer["service"] == "worker"


async def test_read_build_identity_drops_unexpected_fields() -> None:
    """A reader that forwards whatever it finds is one poisoned key from a leak."""
    redis = FakeRedis()
    redis.store["secretaria:build:worker"] = json.dumps(
        {"build_sha": "39a472d", "DATABASE_URL": "postgresql+asyncpg://u:p@host/db"}
    )

    assert await read_build_identity(redis, "worker") == {"build_sha": "39a472d"}


async def test_read_build_identity_survives_garbage() -> None:
    redis = FakeRedis()
    redis.store["secretaria:build:worker"] = "not json at all"
    assert await read_build_identity(redis, "worker") is None
    assert await read_build_identity(FakeRedis(), "worker") is None
    assert await read_build_identity(None, "worker") is None


async def test_redis_failure_never_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observability must never be able to take a service down at startup."""
    monkeypatch.setattr(build_info, "logger", Recorder())

    await publish_build_identity(BrokenRedis(), make_identity())  # must not raise
    assert await read_build_identity(BrokenRedis(), "worker") is None
    assert await check_deploy_parity(BrokenRedis(), make_identity()) == "unknown"


# --------------------------------------------------------------------------
# Worker registry and startup (FIX_01 §7 integration)
# --------------------------------------------------------------------------


def test_worker_registry_is_complete() -> None:
    """Adding or losing a job must fail here, not in production.

    `worker_started` logs exactly these names, so the deployed registry is
    verifiable from the worker's own log — the worker has no HTTP surface.
    """
    assert arq_worker.registered_function_names() == [
        "process_webhook_event",
        "send_cancellation_notice",
        "send_patient_notification",
        "transcribe_audio_message",
        "run_post_booking_hooks",
        "retry_professional_notification",
        "send_transactional_email",
        "process_asaas_event",
    ]
    assert arq_worker.registered_cron_names() == [
        "cron:check_handover_timeouts",
        "cron:send_appointment_reminders",
        "cron:run_onboarding_nudges",
        "cron:run_patient_usage_metering",
        "cron:check_deploy_parity_cron",
    ]


async def test_worker_startup_logs_identity_and_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    monkeypatch.setattr(arq_worker, "logger", recorder)
    monkeypatch.setattr(build_info, "logger", Recorder())
    ctx: dict = {}

    await arq_worker.on_startup(ctx)
    try:
        event, fields = recorder.records[0]
        assert event == "worker_started"
        assert {"build_sha", "built_at", "alembic_head", "source_fingerprint"} <= set(fields)
        assert len(fields["functions"]) == 8
        assert len(fields["cron_jobs"]) == 5

        rendered = json.dumps(fields, default=str)
        for secret in configured_secrets():
            assert secret not in rendered
    finally:
        await ctx["http_client"].aclose()


async def test_deploy_parity_cron_publishes_and_compares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recurring alarm: a divergence introduced AFTER startup still fires."""
    from secretaria.workers.deploy_parity import check_deploy_parity_cron

    recorder = Recorder()
    monkeypatch.setattr(build_info, "logger", recorder)
    redis = FakeRedis()
    # The API announced a different commit while this worker kept running.
    await publish_build_identity(
        redis, make_identity(service="api", build_sha="c55321d", source_fingerprint="1122aabbccdd")
    )

    verdict = await check_deploy_parity_cron({"redis": redis})

    assert verdict == "divergent"
    assert "deploy_sha_divergence" in recorder.events
    # It re-announced itself, so the API's /build does not go blind.
    assert "secretaria:build:worker" in redis.store
