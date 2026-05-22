# SecretarIA

WhatsApp Business + conversational AI service for medical clinics.

SecretarIA is the **WhatsApp + AI core** of a larger SaaS product. It receives
WhatsApp messages, processes them asynchronously, runs the AI logic and
replies. It runs in **Coexistence mode**: the AI (via the Cloud API) and a
human secretary (via the WhatsApp mobile app) share the same phone number.

This repository does **not** contain user auth, billing, a frontend or full
multi-tenancy — those live in other repositories (`platform-api`,
`platform-web`).

## Architecture

| Component | Entry point | Role |
|---|---|---|
| **API** | `secretaria.main:app` | FastAPI. Receives the Meta webhook, validates the HMAC signature, dedupes and enqueues. Returns `200` in well under 5s. |
| **Worker** | `secretaria.workers.arq_worker.WorkerSettings` | arq. Consumes the queue, runs handover + AI logic, sends replies. |
| **Postgres** | — | Conversation state (SQLAlchemy 2.0 + Alembic). |
| **Redis** | — | The arq job queue. |

The API and the worker are the **same Docker image** with different start
commands.

## Tech stack

Python 3.12 · uv · FastAPI · arq · SQLAlchemy 2.0 · Alembic · asyncpg ·
pydantic-settings · httpx · structlog · LangGraph (stub) · ruff · pytest.

## Requirements

- [uv](https://docs.astral.sh/uv/) — installs and manages Python 3.12 itself
- Docker + Docker Compose — local Postgres & Redis
- `make` — optional; every target has a raw `uv` equivalent shown below

### Windows notes

- `make` is often not installed on Windows. If `make` is unavailable, run the
  raw `uv run …` command shown beside each step below.
- SQLAlchemy's async engine depends on `greenlet`, a C++ extension that needs
  the **Microsoft Visual C++ Redistributable** (`msvcp140.dll`). If you see
  `DLL load failed while importing _greenlet`, install it from
  <https://aka.ms/vs/17/release/vc_redist.x64.exe>.

## Local development

### 1. Install dependencies

```sh
make install            # or: uv sync
```

### 2. Create your .env

```sh
cp .env.example .env
```

A `.env` with working local defaults is already included. Fill in the `META_*`
values when you want to test against real WhatsApp.

### 3. Start Postgres + Redis

```sh
make up                 # or: docker compose up -d
```

### 4. Apply database migrations

```sh
make migrate            # or: uv run alembic upgrade head
```

### 5. (optional) Seed a development tenant

```sh
make seed               # or: uv run python scripts/seed_dev.py
```

### 6. Run the API

```sh
make dev                # or: uv run uvicorn secretaria.main:app --reload --port 8000
```

Check it: <http://localhost:8000/health> → `{"status":"ok"}`

### 7. Run the worker (in a second terminal)

```sh
make worker             # or: uv run arq secretaria.workers.arq_worker.WorkerSettings
```

### Verify the webhook handshake

```sh
curl "http://localhost:8000/webhook?hub.mode=subscribe&hub.verify_token=secretaria-dev-verify-token&hub.challenge=12345"
```

Should print `12345`.

### Tests & linting

```sh
make test               # or: uv run pytest
make lint               # or: uv run ruff check . ; uv run ruff format --check .
make format             # auto-format + auto-fix
```

## Database migrations

After changing a model in `src/secretaria/models/`:

```sh
make makemigration m="describe the change"   # autogenerate a revision
make migrate                                 # apply it
```

## Connecting the Meta webhook

In [Meta for Developers](https://developers.facebook.com) → your App →
**WhatsApp → Configuration**:

- **Callback URL**: `https://<your-domain>/webhook`
- **Verify token**: the value of `META_VERIFY_TOKEN`
- **Webhook fields** to subscribe (REQUIRED):
  - `messages` — inbound patient messages
  - `smb_message_echoes` — echoes of the human secretary's messages; this is
    the heart of Coexistence handover

To test locally, expose the API with a tunnel (ngrok / cloudflared).

## .env reference — where to find each value

| Variable | Where to find it |
|---|---|
| `META_APP_SECRET` | Meta App → App Settings → Basic → **App Secret** |
| `META_VERIFY_TOKEN` | A random string **you** choose; must match the dashboard |
| `META_ACCESS_TOKEN` | Permanent **System User** token (Business Settings → Users) |
| `META_PHONE_NUMBER_ID` | WhatsApp → API Setup → **phone number ID** (not the number) |
| `META_GRAPH_API_VERSION` | e.g. `v21.0` |
| `PRECHECK_BASE_URL` | Easypanel: `http://precheck-api:8000` · local: `http://localhost:8001` |
| `PRECHECK_API_KEY` | Shared secret agreed with the Precheck service owner |

For the MVP, use the **Meta test number** — do not connect a real clinic line.

## Deploy on Easypanel

Postgres and Redis are provisioned as **native Easypanel services** — do not
use `docker-compose.yml` in production. Create two app services from this
repository; both build the same `Dockerfile` and differ only in the start
command:

**Service `secretaria-api`**

```
uvicorn secretaria.main:app --host 0.0.0.0 --port 8000
```

**Service `secretaria-worker`**

```
arq secretaria.workers.arq_worker.WorkerSettings
```

Both services share the same environment variables. Point `DATABASE_URL` and
`REDIS_URL` at the internal service DNS names, e.g.:

```
DATABASE_URL=postgresql+asyncpg://USER:PASS@secretaria-postgres:5432/secretaria
REDIS_URL=redis://secretaria-redis:6379/0
```

Run `alembic upgrade head` once after each deploy that adds migrations
(Easypanel one-off command or a release step).

## Project layout

```
src/secretaria/
  main.py            FastAPI app + lifespan (creates the arq pool)
  config.py          Settings (pydantic-settings)
  api/               webhook + health endpoints
  core/              security (HMAC), database, logging
  models/            SQLAlchemy 2.0 models
  schemas/           Pydantic models of the Meta webhook payload
  services/          whatsapp, handover, calendar (stub), precheck
  ai/                LangGraph graph + tools (stubs)
  workers/           arq worker settings + job functions
```

## Status / open TODOs

- **AI graph** (`ai/graph.py`) — stub; returns a fixed reply.
- **Google Calendar** (`services/calendar.py`) — stub (`NotImplementedError`).
- **Precheck endpoints** — marked `PRECHECK_CONTRACT_NEEDED`; confirm with the
  Precheck service owner.
- **Handover timeout** — predicate implemented; periodic cron wiring pending.
- **Outbound rate limiting** and **access-token encryption** — see inline
  `TODO` comments.
