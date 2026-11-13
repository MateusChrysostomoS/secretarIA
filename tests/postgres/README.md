# Real-Postgres concurrency proof (TASK-083)

`test_agenda_concurrency.py` proves the shared agenda's "exactly one winner" guarantee on a real
Postgres: SQLite (the normal suite) has one writer at a time and the advisory locks are no-ops
there. Without `T083_PG_URL` every test here is skipped, so the normal suite is unaffected.

Use a THROWAWAY database only - the tests create and delete their own tenants. Never point it at
a real database and never take credentials from `.env`; the ones below are synthetic.
The fixture refuses to run (a loud failure) unless `T083_PG_URL` has a loopback host AND a
database name starting with `t083`.

```bash
# 1. A throwaway Postgres (any free host port; 55483 here).
docker run -d --name t083-pg -e POSTGRES_USER=t083 -e POSTGRES_PASSWORD=t083pw \
  -e POSTGRES_DB=t083 -p 127.0.0.1:55483:5432 postgres:16
docker exec t083-pg pg_isready -U t083 -d t083   # repeat until "accepting connections"

# 2. Schema: migrations/env.py reads DATABASE_URL (env var, this command only).
export PG=postgresql+asyncpg://t083:t083pw@127.0.0.1:55483/t083
DATABASE_URL=$PG uv run alembic upgrade head
DATABASE_URL=$PG uv run alembic downgrade -1     # optional round trip
DATABASE_URL=$PG uv run alembic upgrade head

# 3. The proof (-s prints the per-scenario counters).
BOT_ALLOWLIST_WA_IDS="" T083_PG_URL=$PG uv run python -m pytest tests/postgres -v -s -p no:cacheprovider
# Optional, not a gate: the broken lock under random jitter alone (no barrier).
BOT_ALLOWLIST_WA_IDS="" T083_PG_NEGATIVE_JITTER=1 T083_PG_URL=$PG \
  uv run python -m pytest tests/postgres -s -k jitter_only -p no:cacheprovider

# 4. Throw it away.
docker rm -f t083-pg
```

What runs: 20 rounds x 8 concurrent writers split across two clinics sharing one person's agenda
(local backend, fake Google, mixed), each in `in_process` mode (production as is) and
`cross_process` mode (no shared in-process lock: only the Postgres advisory locks serialize, as
between two worker processes); concurrent agenda linking; and negative controls that remove the
locks and show the harness then sees more than one winner.
