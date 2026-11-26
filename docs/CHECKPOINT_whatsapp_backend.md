# CHECKPOINT — Conexão do WhatsApp só no backend (TASK-093, Plano 3, etapa a)

Estado: **LOCAL, commitado, sem push, sem deploy.** Deployment: NOT AUTHORIZED.
Branch `task/TASK-093-whatsapp-backend` (base `db86422`). Commits: `136a578` (tabela + flag),
`57ae98c` (serviço), `9866b06` (rotas do hub), `fe3cdcd` (aviso à Brain + cron), `5985bbb`
(teste de logs independente da ordem). Par na brain-api: branch `task/TASK-093-whatsapp-notify`
(`6a35cd8` + docs `537aa86`), `brain-api/docs/CHECKPOINT_whatsapp_backend.md`.

Etapa a = construir sem virar a chave. A virada do site/Portal para este caminho (etapa b)
**não foi feita** e só acontece com pedido explícito do dono. Enquanto isso o caminho atual
(brain-api `POST /doctor/onboarding/attempts`) segue sendo o único em uso.

## Flag

`WHATSAPP_CONNECT_BACKEND_ENABLED` (padrão `False`): as 3 rotas respondem `404` antes da
autenticação (`api/hub/whatsapp.py::_require_backend_enabled`) e o cron não faz nada.
Configurações novas: `META_APP_ID`, `META_ES_CONFIG_ID`. O `META_APP_SECRET` existente (webhook,
mesmo app) é reaproveitado. Flag ligada sem app id/secret → `502 code_exchange_failed` (log
diz `unconfigured`).

## O que entrou

- Tabela `whatsapp_connection_attempts` (`models/whatsapp_connection_attempt.py`, migração
  `e7b3d1f9a5c2`, depois de `f9a1d7c4e2b6`): `tenant_id` (CASCADE), `attempt_id` (64),
  `status` (`pending|connected|failed`, sem CHECK no banco), `phone_number_id`, `waba_id`,
  `failure_code`, `brain_notified_at`; única por `(tenant_id, attempt_id)`. **Nenhum segredo**
  nesta tabela — o token vai cifrado para `tenant_config` via `provisioning.connect_whatsapp`.
- `services/whatsapp_connect.py`:
  - `exchange_code_for_token` / `subscribe_app_to_waba`: espelham a brain-api `meta_graph`;
    nunca levantam exceção; token só em header `Authorization`, nunca em log.
  - `start_attempt` / `complete_attempt`: trava a linha da clínica (`SELECT … FOR UPDATE`);
    tentativa já conectada é devolvida sem chamar a Meta, tentativa que falhou devolve o mesmo
    erro sem chamar a Meta (nova chance = novo `attempt_id`); número de outra clínica é
    recusado **antes** de chamar a Meta; depois troca o código, inscreve o app no WABA e grava.
  - `ConnectError(code)`: `code_exchange_failed`, `waba_subscribe_failed`,
    `phone_number_conflict`.
  - `notify_brain_connected` / `notify_pending_connections`: aviso à Brain e carimbo
    `brain_notified_at`; erro inesperado no aviso nunca desfaz a conexão.
- `services/brain_onboarding.py::notify_whatsapp_connected` → `POST
  {BRAIN_API_BASE_URL}/internal/tenants/{id}/whatsapp-connected` com `X-Internal-Api-Key`;
  devolve `bool`, nunca levanta exceção.
- `workers/onboarding_cron.py::retry_whatsapp_connected_notices` (minutos 3/23/43), registrado
  em `workers/arq_worker.py` (15 → 16 crons).

## Contratos HTTP (hub, `schemas/whatsapp_connect.py`)

| método | rota | quem | resposta |
|---|---|---|---|
| `GET` | `/tenants/me/whatsapp/embedded-signup-config` | gestor | `{app_id, config_id}` (null quando vazio); nunca o segredo |
| `POST` | `/tenants/me/whatsapp/attempts` | gestor | corpo `{attempt_id, code, phone_number_id, waba_id}` (ids da Meta só dígitos; `attempt_id` `[A-Za-z0-9_.:-]{1,64}`; sem campos extras) → `{attempt_id, status, failure_code}`; `409 phone_number_conflict`; `502 code_exchange_failed`/`waba_subscribe_failed`; detalhe `{code, message}` em português simples |
| `GET` | `/tenants/me/whatsapp/connection` | qualquer usuário do hub | `{connected, phone_number_id_masked ("••••1234"), connected_at, channel_released}`; nunca token nem número completo |

`channel_released` lê `services/operating_channel.py::RELEASED_OPERATING_CHANNELS`: conectar
**não** libera o WhatsApp como ambiente da secretária (continua travado, TASK-084).

Corrida no commit (índice único do número) → rollback, tentativa registrada como falha, `409`.
Depois de conectar, a rota avisa a Brain; se a Brain estiver fora, a conexão fica gravada e o
cron reenvia até `200`.

## Provas

- Testes novos: `test_whatsapp_attempt_model.py`, `test_whatsapp_connect.py`,
  `test_hub_whatsapp.py`, `test_whatsapp_connect_notify.py`; RED visto antes de cada parte.
  Provas extras: sem a trava o teste de concorrência falha; sem o ramo de corrida o `409`
  falha; com a flag ligada os testes de `404` falham.
- Meta sempre simulada (`httpx.MockTransport`); Brain simulada. Nada saiu da máquina.
- Migração em Postgres 18 descartável: upgrade → downgrade → upgrade; concorrência real (mesmo
  `attempt_id` duas vezes) → 1 troca de código só.
- Suíte completa em `5985bbb`: **3 failed, 10401 passed, 132 skipped, 1 xfailed**. As 3 são
  antigas (já falhavam na suíte base em `db86422`): 2× `test_service_change_agenda` (falham igual
  num checkout limpo do commit base) e `test_portal_context_opening::test_a_consult_still_in_progress_is_not_followed_up`
  (instável: passa sozinho). Detalhes em `tasks/TASK-093/results/plano3-secretaria.md`.

## Riscos / não provado

- Nada provado contra a Meta de verdade (formato real das respostas).
- `phone_number_id` não é conferido contra o WABA na Meta (igual ao caminho atual da brain-api).
- A trava da clínica fica presa durante as 2 chamadas à Meta (até ~20 s).
- Dois gestores com tentativas diferentes ao mesmo tempo rodam em fila: o último token vale, a
  Brain recebe 2 avisos (cobrança não duplica; 2 e-mails "conectado").

## Ordem de deploy

1. brain-api com `POST /internal/tenants/{id}/whatsapp-connected`.
2. `alembic upgrade head` da secretarIA (`e7b3d1f9a5c2`).
3. `secretaria_api` **e** `secretaria-worker` juntos.
4. Só então, se o dono quiser: ligar `WHATSAPP_CONNECT_BACKEND_ENABLED`, preencher
   `META_APP_ID`/`META_ES_CONFIG_ID`.

Pendências do dono no painel da Meta: domínio autorizado para o SDK, `config_id` do Embedded
Signup, URL do webhook.
