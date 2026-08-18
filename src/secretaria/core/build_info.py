"""Build identity of the running process — the deploy-parity proof (FIX_01 §5.1/§5.2).

The API and the worker are two SEPARATE Easypanel services, deployed manually
from the same repo with no auto-deploy. Deploying one and forgetting the other
is a single missed click, and it presents itself days later as "the
personalisation stopped working": on 2026-08-16 `secretaria-worker` was running
an older commit than `secretaria_api`, so every greeting came from stale code
while the API looked perfectly healthy.

Nothing in the running system could answer "are these two on the same commit?".
This module makes that answerable, and self-announcing:

  * `build_identity()` returns the sanitised identity of THIS process:
    `build_sha` / `built_at` (injected by the build as `ARG`/`ENV` — never read
    from `.git`, which is absent from the image), `alembic_head` (derived from
    the migration scripts shipped INSIDE the image) and `source_fingerprint`.
  * `source_fingerprint` is the field that works with NO build-pipeline support
    at all: it hashes the `secretaria` sources this process actually imported,
    and two services running the same image always agree on it. It is what
    makes parity provable today, where `BUILD_SHA` is not passed at build time.
  * each process publishes its identity to Redis on startup and compares it
    against its peer's, emitting `deploy_sha_divergence` (WARNING) on a
    mismatch — so the omission announces itself in the deploy log of whichever
    service WAS deployed, instead of costing hours of investigation later.

Sanitised by construction: every value produced here is build metadata. No
environment value (other than the two explicitly non-secret build settings), no
DSN, host, token, header or job payload is ever read or emitted.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)

UNKNOWN = "unknown"

ServiceRole = Literal["api", "worker"]
ParityVerdict = Literal["match", "divergent", "unknown"]

_PEER_OF: dict[ServiceRole, ServiceRole] = {"api": "worker", "worker": "api"}

# One Redis key per service role. Deliberately WITHOUT a TTL: the value means
# "the code this service reported the last time it started", which is exactly
# what a deploy-parity question asks — a service that has been idle for a day
# is still deployed at some commit. Overwritten on every startup.
_REDIS_KEY_PREFIX = "secretaria:build:"

# Keys accepted back from Redis. The writer is our own peer, but a reader that
# forwards whatever it finds (into a log line, or out of GET /build) is one
# poisoned key away from leaking something; an allowlist costs nothing.
_PEER_FIELDS = frozenset(
    {"service", "build_sha", "built_at", "alembic_head", "source_fingerprint", "reported_at"}
)

# Bounds the fingerprint walk so a surprising install layout can never turn
# process startup into a filesystem crawl.
_MAX_FINGERPRINT_FILES = 5000

# Longest metadata value kept. A build id is short by nature; anything longer
# is a misconfiguration and must not become an unbounded log field.
_MAX_METADATA_LENGTH = 64


@dataclass(frozen=True, slots=True)
class BuildIdentity:
    """What this process is running. Every field is safe to log and to serve."""

    service: ServiceRole
    build_sha: str
    built_at: str
    alembic_head: str
    source_fingerprint: str

    def as_dict(self) -> dict[str, str]:
        """The full, allowlisted field set — the ONLY shape that leaves here."""
        return {
            "service": self.service,
            "build_sha": self.build_sha,
            "built_at": self.built_at,
            "alembic_head": self.alembic_head,
            "source_fingerprint": self.source_fingerprint,
        }


def _sanitize_metadata(raw: str) -> str:
    """Trim, unquote and bound a build metadata value; empty becomes `unknown`.

    Surrounding quotes are stripped for the same reason `Settings.cors_origins`
    does it: some deploy panels persist a value with the quotes included.
    """
    value = raw.strip().strip("'\"").strip()
    return value[:_MAX_METADATA_LENGTH] if value else UNKNOWN


def _short_sha(raw: str) -> str:
    """Normalise a commit sha to its short form so both sides compare equal.

    A pipeline passing the full 40-char sha and a human typing the 7-char one
    must not read as a divergence — shortening both is simpler and safer than
    prefix-matching at comparison time. Non-sha build ids pass through.
    """
    value = _sanitize_metadata(raw)
    if value == UNKNOWN:
        return UNKNOWN
    lowered = value.lower()
    if len(lowered) > 7 and all(char in "0123456789abcdef" for char in lowered):
        return lowered[:7]
    return value


@lru_cache(maxsize=1)
def _repo_root() -> Path | None:
    """First directory containing `migrations/versions`, or None.

    Checked from the working directory first (`/app` in the image, the repo
    root under pytest) then upwards from this package, which covers an editable
    install where the sources live at `/app/src/secretaria`.
    """
    for base in [Path.cwd(), *Path(__file__).resolve().parents]:
        if (base / "migrations" / "versions").is_dir():
            return base
    return None


@lru_cache(maxsize=1)
def alembic_head() -> str:
    """Head revision of the migration scripts shipped in THIS image.

    This is the migration the deployed code expects — not the revision the
    database is actually stamped at. Comparing those two is a readiness check
    (FIX_06), a different question from this one. Best-effort by design: a
    layout where the scripts cannot be located returns `unknown` rather than
    raising, because a build identity must never be able to stop a process
    from starting.
    """
    root = _repo_root()
    if root is None:
        return UNKNOWN
    try:
        from alembic.script import ScriptDirectory

        heads = ScriptDirectory(str(root / "migrations")).get_heads()
    except Exception as exc:  # fail-open: metadata must never break startup
        logger.warning("alembic_head_unresolved", error=str(exc))
        return UNKNOWN
    # More than one head is itself a fact worth seeing at a glance.
    return "+".join(sorted(heads)) if heads else UNKNOWN


@lru_cache(maxsize=1)
def source_fingerprint() -> str:
    """Hash of the `secretaria` sources this process actually imported.

    The point of a build id is proving two processes run the same code, and a
    `BUILD_SHA` only does that when the pipeline bothers to pass one. This does
    it unconditionally: both Easypanel services run the same image, therefore
    the same bytes, therefore the same hash. `__pycache__` is excluded because
    compiled artefacts carry timestamps and would make identical source hash
    differently.
    """
    package_root = Path(__file__).resolve().parent.parent  # .../secretaria
    digest = hashlib.sha256()
    try:
        paths = sorted(p for p in package_root.rglob("*.py") if "__pycache__" not in p.parts)
        if not paths or len(paths) > _MAX_FINGERPRINT_FILES:
            return UNKNOWN
        for path in paths:
            digest.update(path.relative_to(package_root).as_posix().encode())
            digest.update(path.read_bytes())
    except OSError as exc:
        logger.warning("source_fingerprint_unresolved", error=str(exc))
        return UNKNOWN
    return digest.hexdigest()[:12]


@lru_cache(maxsize=2)
def build_identity(service: ServiceRole) -> BuildIdentity:
    """This process's identity. Computed once, then cached for its lifetime."""
    settings = get_settings()
    return BuildIdentity(
        service=service,
        build_sha=_short_sha(settings.BUILD_SHA),
        built_at=_sanitize_metadata(settings.BUILT_AT),
        alembic_head=alembic_head(),
        source_fingerprint=source_fingerprint(),
    )


def compare_build(local: BuildIdentity, peer: dict[str, str] | None) -> ParityVerdict:
    """Are these two processes running the same code?

    Both `build_sha` and `source_fingerprint` are compared whenever both sides
    carry them, and ANY disagreement wins: a matching sha with a differing
    fingerprint means one of the two labels is lying, which is a divergence.
    A field that is missing or `unknown` on either side is skipped, so a
    deployment that never passes `BUILD_SHA` still gets a real verdict from
    the fingerprint alone. `unknown` means "not comparable", never "fine".
    """
    if not peer:
        return "unknown"
    verdicts: list[ParityVerdict] = []
    for field in ("build_sha", "source_fingerprint"):
        mine = getattr(local, field, "") or ""
        theirs = str(peer.get(field) or "").strip()
        if mine in ("", UNKNOWN) or theirs in ("", UNKNOWN):
            continue
        verdicts.append("match" if mine == theirs else "divergent")
    if "divergent" in verdicts:
        return "divergent"
    return "match" if verdicts else "unknown"


async def publish_build_identity(redis: Any, identity: BuildIdentity) -> None:
    """Announce this process's identity so its peer can compare. Never raises.

    Fail-open at every step: a missing pool (`redis=None`, e.g. the API started
    with Redis down, or a unit test) and a failing write are both swallowed.
    Observability must never be able to take the service down.
    """
    if redis is None:
        return
    payload = {**identity.as_dict(), "reported_at": datetime.now(UTC).isoformat()}
    try:
        await redis.set(_REDIS_KEY_PREFIX + identity.service, json.dumps(payload))
    except Exception as exc:  # fail-open, see docstring
        logger.warning("build_identity_publish_failed", service=identity.service, error=str(exc))


async def read_build_identity(redis: Any, service: ServiceRole) -> dict[str, str] | None:
    """Last identity `service` announced, or None. Never raises.

    The result is filtered to `_PEER_FIELDS` and stringified, so whatever it is
    handed to (a log line, `GET /build`) can only ever see known-safe keys.
    """
    if redis is None:
        return None
    try:
        raw = await redis.get(_REDIS_KEY_PREFIX + service)
    except Exception as exc:  # fail-open, see docstring
        logger.warning("build_identity_read_failed", service=service, error=str(exc))
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode("utf-8", "replace")
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    return {
        key: str(item)[:_MAX_METADATA_LENGTH] for key, item in value.items() if key in _PEER_FIELDS
    }


async def check_deploy_parity(redis: Any, local: BuildIdentity) -> ParityVerdict:
    """Compare this process against its peer and ALARM on divergence.

    `deploy_sha_divergence` at WARNING is the entire point of this module: the
    next time someone deploys one service and forgets the other, the service
    that WAS deployed says so itself, at the top of its own startup log.
    """
    peer_role = _PEER_OF[local.service]
    peer = await read_build_identity(redis, peer_role) or {}
    verdict = compare_build(local, peer or None)
    fields = {
        **local.as_dict(),
        "peer_service": peer_role,
        "peer_build_sha": peer.get("build_sha", UNKNOWN),
        "peer_source_fingerprint": peer.get("source_fingerprint", UNKNOWN),
        "peer_alembic_head": peer.get("alembic_head", UNKNOWN),
        "peer_reported_at": peer.get("reported_at", UNKNOWN),
    }
    if verdict == "divergent":
        logger.warning("deploy_sha_divergence", **fields)
    elif verdict == "match":
        logger.info("deploy_sha_parity", **fields)
    else:
        # Not an alarm: the peer has simply not announced itself yet (first
        # deploy of this code, or Redis unreachable from here).
        logger.info("deploy_sha_unknown", **fields)
    return verdict
