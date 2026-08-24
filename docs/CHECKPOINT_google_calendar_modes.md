# CHECKPOINT — Google Calendar integration modes (per_professional / shared_account)

Validated 2026-08-01 (`uv run python -m pytest -q` blocked on this machine by a Windows
App Control policy — see Nota de ambiente below; ran the equivalent
`python -m pytest -q` via the venv's base interpreter + `PYTHONPATH=src;.venv/Lib/site-packages`
instead) → **1049 passed**, up from a **1019-passed** baseline taken before this round (30
new tests, 0 failures). Same single pre-existing `HTTP_422_UNPROCESSABLE_ENTITY` deprecation
warning as baseline (now emitted twice — one more test path hits the same deprecated FastAPI
constant — nothing new, unrelated to this feature). `ruff check` clean on every changed file.

## Nota de ambiente

`uv run python -m pytest` failed this session with `Uma política de Controle de Aplicativo
bloqueou este arquivo` (os error 4551) — a Windows App Control block on spawning `python` via
`uv run`, reproducible even on a bare `uv run python --version`-style call. Worked around with
the machine's own documented fallback: invoke the venv's base interpreter directly (path from
`.venv/pyvenv.cfg`'s `home`) with `PYTHONPATH=src;.venv/Lib/site-packages`, which resolves to the
exact same virtualenv/site-packages `uv run` would have used:

```
PYTHONPATH="src;.venv/Lib/site-packages" \
  "C:\Users\<user>\AppData\Roaming\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe" \
  -m pytest -q
```

Not a code issue — flag it if `uv run python -m pytest` is still blocked in a future session.

## What was built

Google Calendar integration is now **configurable per tenant**, two modes on
`tenants.google_calendar_mode`:

- **`per_professional`** (default, unchanged behaviour): each professional may connect their
  own Google account (`professional_credentials`); falls back to the clinic's own token. This
  is exactly what every tenant did before this feature — nothing about this path changed.
- **`shared_account`** (new): the clinic connects ONE Google account (existing clinic-level
  OAuth flow, unchanged) and secretarIA creates a **secondary Google Calendar**
  (`calendars.insert`) per professional inside that account, storing the returned `calendarId`
  on `Professional.google_calendar_id` and using it (with the **clinic's** credentials) for that
  professional's create/move/cancel operations. The secondary calendars show up in the clinic
  Google account's own calendar list sidebar, each with its own checkbox/color.

### 1. Mode field

`Tenant.google_calendar_mode: Mapped[str]` (`String(32)`, `server_default="per_professional"`,
`default="per_professional"`) — `src/secretaria/models/tenant.py`, right after
`google_calendar_id`. Named to match the task's own vocabulary; no divergence. Values are NOT a
DB enum — validated only at the Pydantic layer (`Literal["per_professional", "shared_account"]`
on `TenantConfigUpdate`), the exact same convention `pix_retention_policy` already uses (a plain
`String` column + `Literal` validation in the schema, no DB CHECK constraint, no Python enum).

**Migration `c9a1e2f4b6d8`** (revises `b06ff85998bf`, the prior head — confirmed by walking the
full `down_revision` chain across all 18 files in `migrations/versions/`, `alembic heads` itself
was blocked by the same App Control policy as pytest). Purely additive: `op.add_column("tenants",
sa.Column("google_calendar_mode", sa.String(32), server_default="per_professional",
nullable=False))`. `server_default` guarantees every existing row (all current test tenants)
reads as `"per_professional"` with zero backfill and zero behaviour change — nobody's calendar
wiring changes on deploy. **Pending deploy** (not yet applied to any environment).

### 2. OAuth scopes

Added `https://www.googleapis.com/auth/calendar.app.created` alongside the existing
`https://www.googleapis.com/auth/calendar.events` in all three places:

- `src/secretaria/services/calendar.py::SCOPES` (list; used by the token-refresh Credentials
  object, and re-exported to `scripts/check_scopes.py` which imports `SCOPES` directly — that
  diagnostic script needed no edit of its own).
- `src/secretaria/services/google_oauth.py` — renamed the single `CALENDAR_SCOPE` string constant
  to `CALENDAR_SCOPES` (tuple of 2; confirmed via repo-wide grep that nothing external imported
  the old name). `build_authorization_url` now sends `"scope": " ".join(CALENDAR_SCOPES)` — Google's
  OAuth2 endpoint takes multiple scopes as one space-delimited string.
- `scripts/gcal_auth.py::SCOPES` (list, mirrors `calendar.py`).

`google_oauth.py` already used `access_type=offline` + `prompt=consent` (unchanged, confirmed by
reading — no edit needed there). This is what makes **reconnecting** the mechanism by which an
already-connected tenant upgrades a token minted before this feature to carry the new scope — a
refresh token never "gains" a scope on its own. Did **not** add `calendar.calendarlist` per
explicit instruction (no `calendarList.list` / agenda-reuse UI built).

### 3. Secondary calendar creation

`CalendarService.create_secondary_calendar(summary: str) -> dict` (`services/calendar.py`) — a
thin, pure `calendars().insert(body={"summary": ..., "timeZone": ...}).execute()` wrapper, same
blocking-to-thread + HttpError-translation pattern as every other method on the class. No
`calendars.delete` anywhere in this codebase (by design — see routing rule below).

Orchestration lives in `services/tenant_config.py::ensure_professional_secondary_calendar(session,
tenant, professional) -> SecondaryCalendarResult` (`professional_id`, `google_calendar_id`,
`created: bool`):

- Professional already has `google_calendar_id` → returned as-is, `created=False`. Never calls
  `calendars.insert` twice for the same professional (true idempotency — the fake-Google test
  asserts the insert call count stays zero on repeat).
- Clinic has no connected Google account → `ClinicCalendarNotConnectedError`.
- Builds its own minimal `TenantRuntimeConfig` directly off the `tenant`/clinic-token DB reads —
  **deliberately not** `load_tenant_config()`, which for a tenant with exactly one active
  professional may resolve `google_refresh_token`/`google_calendar_id` *through* that
  professional. Bypassing it means the secondary calendar is always created inside the clinic's
  own account, never a professional's, regardless of roster size.
- Summary sent to Google: `f"{professional.name} — {tenant.clinic_name}"`.

**Endpoint**: `POST /tenants/me/professionals/{professional_id}/calendar`
(`api/hub/professionals.py`, same router/prefix as the rest of the professionals CRUD). Auth =
same `get_current_tenant` dependency as every other hub endpoint. Ownership = existing
`_get_professional` helper (404 for an unknown id or one owned by another tenant). **Never**
entitlement-gated (core platform wiring, not an addon — same "config save always allowed"
principle as `PUT .../config`).

**Contract:**
- Request: no body.
- Success: `200 OK`, `ProfessionalCalendarConnect` — `{"professional_id": str, "google_calendar_id":
  str, "created": bool}`. Status is `200` in **both** the created and idempotent-no-op case
  (kept uniform on purpose — `created` in the body is the disambiguator; no `201`/`200` split).
- `404` — professional id unknown or belongs to another tenant (plain string detail, existing
  convention, unchanged).
- `422` — clinic has no Google Calendar connected: `{"detail": {"code":
  "clinic_calendar_not_connected", "message": "A clínica ainda não conectou uma conta do Google
  Calendar. Conecte a agenda da clínica antes de criar agendas por profissional."}}`.
- `409` — stored clinic token predates the new scope: `{"detail": {"code":
  "google_reconnect_required", "message": "A conexão da clínica com o Google Calendar não tem
  mais a permissão necessária para criar agendas. Reconecte a conta Google da clínica para
  continuar."}}`.

**Note on error shape**: this is a **new** convention for this repo — every other hub endpoint's
`HTTPException.detail` is a plain snake_case string (e.g. `"multi_professional_not_entitled"`),
not a `{"code", "message"}` dict. Introduced here specifically because the task asked for both a
machine-readable `code` (frontend UI trigger) and a pt-BR `message` in the same response;
pre-existing endpoints are untouched and keep the plain-string convention. Worth a deliberate
look before reusing this shape elsewhere.

### 4. Routing rule (event create/move/cancel) — the credential × calendar_id edge case

**Rule chosen**: routing is **mode-driven**, not purely "does an own token exist":

- **`shared_account`** — the clinic's own credentials are **always** used for a professional-scoped
  Calendar operation, even when that professional also happens to have their own connected token
  (a leftover `per_professional` connection, or simply connected before/after a mode switch —
  switching modes never clears tokens). Rationale: a `google_calendar_id` created by
  `ensure_professional_secondary_calendar` exists **only** inside the clinic's account; pairing it
  with the professional's own token 404s. Rather than trying to tag *which* calendar_id is
  clinic-owned vs. professional-owned, the mode itself is the discriminator — simpler and matches
  the product design (shared_account's whole point is "every professional lives inside the one
  connected account").
- **`per_professional`** (default) — **completely unchanged**. Verified against the actual current
  code before touching anything: a professional's own token and own (independently, manually
  configured) `google_calendar_id` were already an orthogonal, supported combination (e.g. a
  doctor who connects their own Google account **and** pastes in a secondary calendar id from
  that same account). This combination keeps working exactly as before.

**Implementation**: one new private helper, `services/tenant_config.py::_professional_credential(tenant,
own_token, clinic_token) -> str | None` — `shared_account` → `clinic_token`; else →
`own_token or clinic_token` (the pre-existing expression, byte-for-byte). Applied at **both**
places that resolve a professional's credential (previously duplicated logic, now sharing this
one rule so they can't disagree):

- `resolve_professional_calendar()` — used by `workers/tasks.py` (flow-router booking, manage/
  reschedule-owner resolution, action-button cancel) and `plugins/multi_professional.py` (LLM
  tool path). Fixing this one function covers all of those call sites with zero further edits.
- `load_tenant_config()`'s single-active-professional branch — the shortcut that resolves
  `TenantRuntimeConfig.google_refresh_token`/`google_calendar_id` straight through a tenant's
  **sole** active professional. This is a **separate**, previously-duplicated bit of logic (not a
  call to `resolve_professional_calendar`) and feeds `CalendarService.from_tenant_config(...)` in
  `ai/graph.py`, `api/hub/calendar.py`, `api/admin/tenants.py`, and
  `services/payments/deposit_lifecycle.py` — almost certainly the **most common real-world path**
  today, since most secretarIA clinics are single-professional. Left unfixed, this specific branch
  would have silently reintroduced the exact bug the routing rule exists to prevent, for the most
  common tenant shape.

`google_calendar_id` itself is **never withheld** by the rule in either mode — only *which
credential* it gets paired with changes. `ensure_professional_secondary_calendar` also does not
rely on the routing rule or on `load_tenant_config` at all (see item 3) — it builds its own
minimal clinic-only config directly off the DB row, so it can never accidentally inherit a
professional-substituted token.

### 5. Scope-insufficient 403 → structured reconnect signal

New `GoogleScopeInsufficientError` (`services/calendar.py`), distinct from the existing
`CalendarUnavailableError`: the fix for this one is "reconnect the clinic's Google account", not
"wait and retry" or "hand off to a human". `_raise_if_scope_insufficient(exc)` is checked
**before** the existing `_raise_if_unavailable(exc)` inside `create_secondary_calendar` — a plain
403 is still the generic outage mapping (unchanged for every other method on the class); only a
403 that also carries a scope-insufficient marker is intercepted first.

Detection matches (case-insensitive substring) against several known Google error-body markers —
`insufficientPermissions` (classic `errors[].reason` shape) and `insufficient authentication
scopes` / `ACCESS_TOKEN_SCOPE_INSUFFICIENT`-style wording (newer `status`/`message`-only shape) —
scanned across the HttpError's `.reason`, `.error_details`, and raw `.content`, because the exact
JSON shape Google returns for this case could not be pinned down from this dev/test environment
(no live credentials to provoke it against the real API). If a real-world 403 ever slips past
both markers, it still degrades safely to the pre-existing `CalendarUnavailableError` outage
path — never an unhandled 500 either way.

Mapped to the hub's `409 google_reconnect_required` at the `POST .../calendar` endpoint (see
item 3's contract). The regular event-path endpoints (create/move/cancel appointment,
`api/hub/calendar.py`) use only the pre-existing `calendar.events` scope, already granted to
every connected tenant — they cannot produce this new error class, so they were not touched.

### 6. Mode endpoint

Extended the existing tenant config surface (`GET`/`PUT /tenants/me/config`) — no new route.
`google_calendar_mode` added to `schemas/config.py`'s `TenantConfigUpdate` (`Literal["per_professional",
"shared_account"] | None`, free 422 on any other value) and `TenantConfigRead` (`str`, always
present), and to `api/hub/config.py`'s `_SCALAR_FIELDS` tuple + `_read_model`. Because
`update_config` only ever `setattr`s fields present in `_SCALAR_FIELDS` that arrived in the
request body (`exclude_unset`), and never touches any `Professional` row from this endpoint at
all, switching modes is **structurally** non-destructive — there is no code path here that could
touch a token or a `google_calendar_id`, tenant's or professional's.

### 7. Professionals roster payload

**No change needed** — `ProfessionalListItem` (`GET /tenants/me/professionals`) already returns
`google_calendar_id: str | None` per row (pre-existing field). That alone is what the frontend
needs for the shared_account validation badge: `google_calendar_id != null` ⇒ this professional
has a dedicated calendar. No secret/token ever rides along (`ProfessionalListItem` is a
whitelisted schema, no `*_encrypted` field exists on it to leak). `has_calendar` is untouched and
keeps its current semantics (`tenant-level token OR this professional's own token`) — this
remains an accurate "is this professional schedulable at all" signal in `shared_account` mode too:
once the clinic is connected, every professional is coverable even before its own secondary
calendar exists yet (events land on the clinic's primary calendar until
`POST .../calendar` is called for them).

### 8. `api/hub/` structure

**Already a subpackage** (`analytics.py`, `calendar.py`, `config.py`, `conversations.py`,
`deps.py`, `oauth.py`, `professionals.py`, `units.py` — 8 files) — `CLAUDE.md`'s note describing
it as still-flat "config.py, oauth.py, calendar.py, deps.py" (with a "group into `api/hub/` next
time touched" instruction) is **stale**; that restructuring evidently already happened in an
earlier session and the doc wasn't updated to say so. Worth a one-line `CLAUDE.md` fix in a future
session (not done here — doc hygiene, not this feature). This session only added code to two
pre-existing files inside the subpackage (`config.py`, `professionals.py`); no new file, no
threshold newly crossed, no restructuring performed (per instruction, and moot anyway).

## Testing

30 new tests, all green, no existing test modified beyond additive extensions:

- `tests/test_professional_resolution.py` — `_seed()` gained an optional `mode=` kwarg (default
  `"per_professional"`, every existing call site unaffected). New: default-mode model test;
  `shared_account` + own token + clinic-created calendar_id → clinic token used; same but
  *before* the calendar_id exists yet (still clinic token, not the professional's own primary);
  explicit `per_professional` regression guard (own token + own calendar_id still wins — mirrors
  the pre-existing `test_resolver_uses_professionals_own_config`, which itself required no
  changes and keeps passing as the implicit default-mode regression guard).
- `tests/test_professional_config.py` — the same `shared_account`-uses-clinic-token and
  `per_professional`-regression pair, but through `load_tenant_config`'s single-active-professional
  branch specifically (the separate code path described in item 4).
- `tests/test_calendar_unavailable_mapping.py` — `SCOPES` contains both scopes; scope-insufficient
  403 detection (both known Google error-body shapes) maps to `GoogleScopeInsufficientError`; a
  generic/marker-less 403 and non-403 statuses do **not**; `create_secondary_calendar` end-to-end
  against a fake Google service — scope-insufficient, generic-403-still-CalendarUnavailableError
  (regression guard for the existing outage mapping), network failure, and success (asserts the
  request body's `summary`, returns the inserted resource verbatim).
- `tests/test_hub_config.py` — GET default (`per_professional`); PUT round-trip to
  `shared_account`; invalid value → 422 (value left untouched); plain save works while
  disconnected (same invariant as every other config field); non-destructiveness (token +
  `google_calendar_id` both survive a mode PUT unchanged).
- `tests/test_hub_professionals.py` — new `POST .../calendar` section with a fake
  `CalendarService` monkeypatched onto `services.tenant_config.CalendarService` (no real Google
  call in any test): creates + persists (asserts the DB row, not just the response, and the exact
  summary string sent); idempotent no-op (asserts the fake's insert was never called a second
  time); clinic-not-connected → structured 422; scope-insufficient → structured 409 (and the
  professional row is confirmed untouched on failure); unowned professional → 404; unknown id →
  404.
- `tests/test_hub_oauth.py` — `GET /tenants/me/calendar/oauth/start` requests both scopes
  (space-delimited `scope` query param decoded and checked for both URLs).

## Pré-requisitos do usuário (ação manual antes/depois do deploy)

**(a) Google Cloud Console — projeto secretarIA (`secretaria-496912`).** Publishing status is
**"Testando"** as of 2026-07-31, which makes this trivial: Google Auth Platform → **Acesso a
dados** → **"Adicionar ou remover escopos"** → add `https://www.googleapis.com/auth/calendar.app.created`
(today only `calendar.events` is registered there). No verification/review needed while in
Testing mode.

**(b) Every already-connected tenant must RECONNECT the clinic's Google account** (`GET
/tenants/me/calendar/oauth/start` → consent → callback) to upgrade its stored refresh token to
the new scope. A refresh token never gains a scope on its own — this is unavoidable and applies
regardless of which mode a tenant ends up choosing (even a `per_professional` tenant that never
touches `shared_account` doesn't need to reconnect *for this feature*, since it never calls
`calendars.insert` — reconnection is only required for a tenant that actually wants to use
`shared_account` mode).

**(c) Manual e2e validation roteiro (post-deploy):**
1. Apply migration `c9a1e2f4b6d8` (`alembic upgrade head`).
2. Add the scope in Google Auth Platform (step a above) if not already done.
3. In the hub, `PUT /tenants/me/config {"google_calendar_mode": "shared_account"}` for a test
   tenant; confirm `GET` reflects it.
4. If that tenant's clinic Google connection predates this deploy, reconnect it (step b) — confirm
   `GET /tenants/me/config`'s `calendar_connected` stays `true` throughout.
5. `POST /tenants/me/professionals/{id}/calendar` for a professional with no `google_calendar_id`
   yet. Expect `200` with `created: true` and a fresh `google_calendar_id`. If instead a `409
   google_reconnect_required` comes back, that confirms step 4 wasn't done (reconnect, then
   retry) — this IS the intended, structured signal, not a bug.
6. Open the clinic's Google Calendar in a browser — the new secondary calendar should already be
   visible in the left sidebar, named `"<professional name> — <clinic name>"`.
7. Call the same endpoint again for the same professional — expect `200` with `created: false`
   and the SAME `google_calendar_id` (idempotency), and confirm no second calendar was created in
   the Google UI.
8. Book/move/cancel an appointment for that professional (WhatsApp bot flow, or the hub's
   `POST/reschedule/cancel .../calendar/appointments`) and confirm the event lands in the new
   secondary calendar, not the clinic's primary.
9. **Operational note**: while the OAuth consent screen is in **Testing** mode, Google expires
   refresh tokens after **~7 days** — a known limitation, not specific to this feature. Any
   `shared_account` tenant's clinic connection (and therefore every professional booking under
   it) will need reconnecting on that cadence until the app is published/verified, at which point
   this limitation disappears. Worth keeping in mind during any extended manual validation window.

## Decisões tomadas

- Endpoint path/method/response shape matched the task's own suggestion exactly
  (`POST /tenants/me/professionals/{id}/calendar`, no divergence).
- `{"code", "message"}` dict error shape is new for this repo (see item 3 note) — used only for
  the two new error paths this feature introduces; every pre-existing endpoint's plain-string
  `detail` convention is untouched.
- `POST .../calendar` returns `200` uniformly (not `201` on first creation) — `created` in the
  body is the disambiguator. Simpler for the frontend than branching on status *and* body for the
  same logical shape.
- Routing rule is mode-driven (see item 4), not "does an own token exist" — chosen specifically
  because a purely token-presence-driven rule would have silently changed `per_professional`'s
  existing own-token-plus-own-calendar_id behaviour, which this session confirmed is real,
  supported, orthogonal configuration today.
- `calendar.calendarlist` scope intentionally not requested; no agenda-reuse/listing UI built
  (explicit instruction, minimizes consent friction).

## Pendências

- Migration `c9a1e2f4b6d8` not yet applied anywhere (dev/staging/prod).
- Google Auth Platform scope registration (prerequisite a) not yet done.
- No existing tenant has reconnected yet (prerequisite b) — irrelevant until a tenant actually
  opts into `shared_account` mode.
- Hub UI (mode toggle, "Reconectar agenda" button wired to `google_reconnect_required`,
  per-professional "Criar agenda" action, the roster's `google_calendar_id`-derived validation
  badge) ships separately in `brain-frontend` — not this repo, not this session.
- `CLAUDE.md`'s "Doctor hub / CRM... this cluster has crossed the threshold, group it into
  `api/hub/` next time it is touched" note is stale (already done) — cosmetic doc fix, not
  addressed this session (out of scope, no code/behaviour impact).

---

## 9. Criação em lote — "Conta única" passou a fazer alguma coisa (2026-08-24)

### O problema, relatado pelo usuário

> "veja se está implementado corretamente a troca da configuração para o Google
> Calendar quando vai de 'Por profissional' para 'Conta única' e vice-versa de
> modo que quando esta em contas única, tem que criar uma agenda interna para
> cada médico na conta do google e testei e isso não ocorreu"

**Não era um bug — era uma parte que nunca foi construída.** O item 6 acima
descreve o `PUT` de modo como deliberadamente inerte ("estruturalmente não
destrutivo"), e o item 3 entrega a criação como um `POST` **por profissional**.
Nada ligava as duas coisas. Trocar o modo e salvar criava exatamente zero
agendas, por design — e do lado de quem usa, isso é indistinguível de quebrado.

### O que entrou

**`POST /tenants/me/professionals/calendars`** (`api/hub/professionals.py::
create_professional_calendars`): cria a agenda secundária de **todo profissional
ativo que ainda não tem uma**, dentro da conta Google da clínica. Reusa
`ensure_professional_secondary_calendar` sem mudá-la — mesma idempotência, mesmo
`calendars.insert`, mesmo `summary`.

Resposta `200` com relatório por linha
(`ProfessionalCalendarBulkResult`): `created`, `already`, `failed`, e `items[]`
com `professional_id`, `name`, `google_calendar_id`, `created`, `error`.

**Por que 200 com relatório em vez de um status só:** as agendas que deram certo
**já existem no Google**. Transformar a corrida inteira em erro por causa de uma
linha jogaria fora ids de agendas reais, e o usuário precisa ver exatamente quais
médicos ainda faltam.

**Duas condições são da CLÍNICA, não de um profissional**, e por isso derrubam a
requisição inteira em vez de repetir o mesmo erro em cada linha:

| Situação | Status | `code` | Commit? |
|---|---|---|---|
| Clínica sem conta Google conectada | 422 | `clinic_calendar_not_connected` | Não — nada foi criado (a checagem roda antes da primeira chamada ao Google) |
| Token da clínica anterior ao escopo `calendar.app.created` | 409 | `google_reconnect_required` | **Sim** — commita o que já tinha dado certo antes de abortar |

Esse `await session.commit()` antes do 409 é deliberado e tem teste próprio
(`test_bulk_scope_error_is_409_but_keeps_what_already_succeeded`): sem ele, uma
agenda que o Google já criou ficaria órfã, sem id em lugar nenhum.

Qualquer outra falha (indisponibilidade do Google no meio) fica na linha, com um
**código** — nunca o texto da exceção, que pode carregar dados da conta da
clínica.

**Não é gated no modo**: o hub chama isto logo depois do save que grava o modo, e
recusar com base num valor que o mesmo cliente acabou de escrever é uma corrida
sem ganho nenhum. Uma agenda secundária é inerte em `per_professional` de todo
jeito — quem decide se `google_calendar_id` é pareado com a credencial da clínica
é a regra de roteamento do item 4.

### Profissional que ENTRA numa clínica em `shared_account`

`_ensure_calendar_for_new_professional`, chamado no fim de `POST
/tenants/me/professionals`. **Best effort, depois do commit do profissional**: a
linha é real independente do humor do Google, e os dois caminhos de retentativa
(o lote e o botão por linha) são idempotentes. Toda falha é logada e engolida —
inclusive "a clínica não conectou o Google", que para um tenant em onboarding é o
caso normal, não um erro.

Armadilha encontrada na execução, vale registrar: `session.rollback()` **expira
todas as instâncias da sessão**, então ler `professional.name` depois dele dispara
IO preguiçoso e estoura `MissingGreenlet` dentro do handler de exceção. Os ids são
capturados **antes** do `try`, e o rollback é seguido de um `session.refresh` —
o caller ainda precisa serializar a linha. É a mesma armadilha que
`api/hub/config.py` já documenta.

### O lado do frontend

O hub chama o lote **depois** de um save bem-sucedido cujo modo é
`shared_account` (`configuracao/lib/save.ts::ensureCalendars`). Ordem testada:
`put` e só então `calendars` — criar agendas para um modo que o servidor recusou
deixaria a conta Google da clínica com agendas de uma configuração que nunca
entrou em vigor. Uma falha do Google **nunca** transforma um save bem-sucedido em
erro: a configuração já está gravada, e mandar o usuário salvar de novo o que já
está no ar é pior que o silêncio. O que aparece é o número de agendas criadas (ou
que faltaram) no toast.

O botão "Criar agenda do profissional" por linha continua existindo, como
retentativa.

### Testes

12 novos em `tests/test_hub_professionals.py`: cria uma por profissional ativo;
ignora inativo; idempotência (segunda corrida não chama `calendars.insert` de
novo para ninguém); 422 sem clínica conectada e **nada** criado; o 409 que
preserva o que já deu certo; falha de uma linha sem custar as outras (e sem vazar
a mensagem do Google); roster vazio; `/calendars` não colide com
`/{id}/calendar`; e os três casos do profissional que entra (cria em
`shared_account`, não cria em `per_professional`, falha do Google não impede a
criação da linha).
