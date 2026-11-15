# CHECKPOINT — Ambiente da secretarIA (Portal Brain × WhatsApp) — TASK-084

Data: 2026-11-14. Tarefa: `BRAIN/tasks/TASK-084/` (SPEC.md = desenho aprovado, TASK.md = estado).
Branch `task/TASK-084-operating-channel` (worktree `BRAIN-worktrees/TASK-084/secretarIA`), commits LOCAIS,
sem push, sem deploy.

## 1. O que o dono pediu (2026-11-14)

"Faça todo o código para que seja possível alterar o ambiente de funcionamento entre Portal Brain e WhatsApp,
mas que hoje esse toggle seja apenas visual e que não seja possível mudar para o WhatsApp […] é necessário ter
esse atributo no schema do banco de dados da entidade Clínica e seu contexto". E o aviso "dois nomes ficariam
iguais no WhatsApp" só para clínica no WhatsApp — nunca para clínica no Portal.

## 2. O que existe agora (este repo)

- **Coluna** `tenants.operating_channel` (`VARCHAR(16) NOT NULL DEFAULT 'brain_message'`, CHECK
  `ck_tenants_operating_channel` em `brain_message`/`whatsapp`). Mesmo vocabulário de `Patient.channel`
  (`models/tenant.py::OPERATING_CHANNEL_*`, fixado igual a `services/channel_sender.py::CHANNEL_*` por teste).
  Toda clínica existente e nova fica em `brain_message` (Portal Brain).
- **Migração** `b8d4f2a6c9e1` (sobre `d2f6b8a4c1e7`), em batch mode (roda no SQLite dos testes; no Postgres são
  os ALTERs simples, sem cópia de tabela). Downgrade remove CHECK e coluna.
- **Chave de liberação** `services/operating_channel.py::RELEASED_OPERATING_CHANNELS = ("brain_message",)` — o
  ÚNICO lugar que decide o que a clínica pode escolher. Constante de código de propósito (não variável de
  ambiente): liberar exige código de qualquer jeito, e ninguém abre isso pelo painel por engano.
  `available_operating_channels()` (ordem fixa) e `set_operating_channel()` (mesmo valor = no-op; canal não
  liberado = `OperatingChannelUnavailable`; valor desconhecido = `ValueError`).
- **Leitura**: `GET /tenants/me/config` (e toda resposta que usa `TenantConfigRead`) traz `operating_channel` e
  `available_operating_channels`.
- **Escrita**: `PATCH /tenants/me/operating-channel` `{"operating_channel": "..."}` (chave obrigatória, nada além
  dela) — só gestor (`require_clinic_manager`: `manager`, ou `doctor` com `is_owner`/`is_manager`; senão 403
  `forbidden_role`/`role_claim_missing`). Canal não liberado → **409**
  `{"code": "operating_channel_unavailable", "message": "Esta opção ainda não está disponível."}`, nada gravado.
  Mudança registra `operating_channel_changed` (`tenant_id`, `previous`, `channel`; sem PII).
- **Escritor único**: `PUT /tenants/me/config` e `PUT /tenants/me/configuration` ignoram a chave (teste fixa).
- **Nada no caminho das mensagens lê o canal ainda**: a conversa continua respondida pelo canal em que chegou
  (`workers/shared/sender.py::_reply_sender`). Nenhum worker mudou — deploy do worker só por paridade.
- **Vetores compartilhados** `tests/test_whatsapp_title_collision_vectors.py`: quais nomes de serviço ficam
  iguais na lista do WhatsApp (corte de 24 + 🏥 + `_norm`) e qual o toque agenda. O Brain-Message-Frontend usa
  os MESMOS vetores para o aviso (`lib/__tests__/whatsapp-title-collisions.test.ts`) — mudou o corte aqui,
  mude lá.

## 3. Deploy (NÃO autorizado — ordem quando for)

1. `alembic upgrade head` (migração `b8d4f2a6c9e1`) com os serviços ainda velhos.
2. `secretaria_api` + `secretaria-worker` juntos (`GET /build` → `deploy_parity`).
3. Brain-Message-Frontend (o bloco "Onde a secretarIA atende" e as regras de nome).

Rollback: código velho nos dois serviços primeiro, depois `alembic downgrade`.

**Outras branches com migração:** TASK-083 (`e4a8c2f6b1d9`) também nasce em `d2f6b8a4c1e7`, e a TASK-074
(`c4e8a1d6b3f9`) tem base própria. Quem entrar depois desta precisa de uma revisão `alembic merge` (ou
re-parent) para a cadeia continuar com uma cabeça só (`tests/test_migration_*` acusam duas cabeças).

## 4. Quando o WhatsApp for liberado — checklist (não esquecer)

1. Pôr `"whatsapp"` em `RELEASED_OPERATING_CHANNELS`. A tela libera a opção sozinha (ela só habilita o que o
   servidor lista).
2. Decidir o que o canal PASSA A CONTROLAR: roteamento, porta do Portal (`brain-api` `brain_message_enabled`),
   conexão do número (embedded signup / `phone_number_id`), confirmação antes de trocar, o que acontece com as
   conversas abertas no Portal.
3. **Conserto do toque no WhatsApp**: linhas `svc|` e `ins|` são resolvidas pelo TÍTULO visível
   (`schemas/webhook.py::_PAYLOAD_ROW_PREFIXES` não tem `svc|`/`ins|`), então dois nomes iguais nos 23 primeiros
   caracteres agendam o primeiro (`flow_router._match_service` / `booking_scope.canonical_service_name`). Levar
   o nome inteiro/id no toque, como `prof|`/`day|` já fazem, cobrindo cartões já enviados. Receita original e
   reprodução: `BRAIN-worktrees/TASK-082/_local-test/wa_collide.py`; vale para as listas de serviço do
   agendamento e do "alterar consulta".
4. **Convênio tem o mesmo risco** (`ins|` + `_match_insurance_plan` por título): estender o aviso aos nomes de
   convênio se o item 3 não sair antes.
5. Outras menções ao WhatsApp que o /contexto ainda mostra a TODA clínica (fora dos campos de nome): "Avisos
   pagos do WhatsApp" (lembretes), rodapé da simulação, nota "botões do WhatsApp e do Portal" do sinal no
   convênio.
6. `secretarIA-frontend` `/configuracao` (legado) mantém as regras de 24 para todos.

## 5. Provas

Ver `BRAIN/tasks/TASK-084/TASK.md` ("Validation"): testes novos, suíte completa em partes, migração
upgrade/downgrade num Postgres descartável e a prova no navegador (pilha local sintética
`BRAIN-worktrees/TASK-084/_local-test`).
