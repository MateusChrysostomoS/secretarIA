# Lembretes, primeira mensagem e estado de confirmação da consulta — Design

**Data:** 2026-10-03 · **Task:** TASK-032 · **Status:** aguardando revisão do dono (spec escrita; planos só depois da aprovação)
**Origem:** conversa de 2026-10-03: o dono não viu nenhuma mensagem automática para quem já tem consulta marcada.
**Mapa do código usado como base:** relatório de exploração de 2026-10-03 (lembretes, abertura da conversa, estado da consulta, reaproveitáveis, liberar horário, visibilidade da clínica, limites de canal) — resumido na §3. Funções citadas são âncoras estáveis, sem número de linha.

## 1. Objetivo e intenção

Quem tem consulta marcada hoje não recebe nada que o ajude a **confirmar**, e a clínica não enxerga quem confirmou. Queremos:

1. **Três lembretes** por consulta — um configurável pela clínica (ex.: 5 dias antes) e dois fixos (1 dia e 1 hora antes) — todos com a mensagem "LEMBRE-SE: Sua consulta com {médico} está marcada para o dia {DD/MM/AAAA} às {HH:MM} para {serviço}. {requisitos}" e os botões **Confirmar / Cancelar / Outro**.
2. **A mesma mensagem como primeira mensagem do chat** quando o paciente com consulta marcada abre a conversa ou escreve depois de um silêncio.
3. **"Cancelar" abre um caminho:** remarcar esta consulta, marcar uma consulta totalmente diferente, ou não ir mais.
4. **Um estado de confirmação que a clínica vê na agenda**: verde quando confirmou (✓), verde com ✓✓ quando confirmou duas vezes, neutro quando só está marcada, **vermelho** quando um lembrete saiu, o prazo de aviso passou e o paciente não confirmou — com ações para a clínica agir, inclusive **liberar o horário**.

**Dito pelo dono (2026-10-03):** o paciente só precisa confirmar **uma vez** para a cor mudar; confirmando mais de uma vez aparece uma sequência de vês verdes no evento; depois de **duas** confirmações param os **pedidos de confirmação**, mas os lembretes de 1 dia e 1 hora continuam; a agenda não deve mostrar só o que o Google Calendar mostra.
**Suposição (a corrigir se estiver errada):** a liberação de horário é sempre decisão da clínica (um clique); **nada é liberado automaticamente** nesta versão.

## 2. Decisões do dono (2026-10-03)

1. **Escopo:** as quatro peças — mensagem de lembrete como primeira mensagem + caminho do Cancelar; estado de confirmação visível; lembretes proativos com o mesmo conteúdo e botões; aviso e liberação do horário.
2. **Abordagem:** um **cronograma de lembretes por consulta** guardado no banco (uma linha por lembrete), em vez de estender o job atual por janelas de horário.
3. **Regra de parada:** depois de duas confirmações param só os **pedidos de confirmação** (botões e a mensagem de abertura do chat); os lembretes de 1 dia e 1 hora saem como lembrete simples, sem botão de confirmar.
4. **Liberar horário:** a clínica é avisada e libera com **um clique** na agenda (apaga o evento do Google, avisa o paciente com botões para remarcar, segue as regras do Pix). Os prazos de aviso: **2 h** depois do lembrete configurável, **2 h** depois do de 1 dia e **20 min** depois do de 1 hora, se o paciente não confirmou.
5. **Lembretes por e-mail para o Portal** (paciente sem telefone): e-mail com o mesmo texto e um botão que abre a conversa, onde a mensagem espera como primeira mensagem.
6. **Texto:** inclui o horário ("às HH:MM"), além do dia; para consulta marcada para outra pessoa, o texto nomeia o atendido.
7. **"Marcar outra consulta"** só cancela a consulta original **depois** que a nova for confirmada.
8. **Configuração por clínica:** lembrete extra desligado até a clínica configurar; a funcionalidade inteira atrás de interruptor por clínica, desligado por padrão.

## 3. Estado atual (fatos do mapa que sustentam o design)

- **Lembrete de hoje** (`plugins/reminders.py`, cron `send_appointment_reminders`, a cada 5 min): janelas de 24 h e 1 h; só WhatsApp; texto de uma linha "Lembrete: você tem {serviço} agendado(a) para {DD/MM/AAAA às HH:MM} na {clínica}." — **sem médico, sem requisitos e sem botões** (os três botões `apptconfirm|`/`apptresched|`/`apptcancel|` só existem para consulta com sinal Pix pago). Fora da janela de 24 h usa o modelo aprovado `appointment_reminder` com uma variável. Envio que falha **se perde** (a marca de "já enviado" é gravada antes e nunca liberada). A chave da marca não inclui o horário: **remarcar depois do lembrete de 24 h não gera novo lembrete**. Consulta marcada com menos de 24 h de antecedência nunca recebe o de 24 h.
- **Portal (`brain_message`):** nenhum lembrete é entregue (o job usa `patient.wa_id`, nulo para eles); a marca é gravada e o erro se repete a cada varredura. `BrainMessageSender.send_template` recusa de propósito. Botões de ação não são decodificados no Portal.
- **Abertura do chat** (`workers/turn_router.py::_route_inbound_turn`, `workers/shared/greeting.py`): o estado `HAS_UPCOMING`/`HAS_UPCOMING_SOON` (`services/patient_context.py`) gera "Vi aqui que você já tem uma consulta marcada para {DD/MM às HH:MM}, com {médico}", requisitos e botões `[Remarcar, ❌ Cancelar, Outro]` — mas **só quando é a primeira mensagem da conversa inteira**, o que na prática nunca acontece (o consentimento vem antes). Depois de 6 h de silêncio (`reactivation_gap_minutes`, `workers/shared/state_expiry.py::_reactivation_offer`) o paciente vê um menu genérico sem saber da consulta. A abertura do Portal (`workers/portal/open.py`) nunca lê o estado.
- **Consulta:** `Appointment.status` com `scheduled`, `confirmed`, `rescheduled` (vivos) e `cancelled`, `attended`, `no_show`. `confirmed` só é gravado pelo botão do lembrete com Pix e por `PATCH` manual do hub; **não tem data**, é sobrescrito pelo `rescheduled` e vice-versa, e **não há contador de confirmações**. Estado de lembrete existe só como linhas de `ProcessedEvent`.
- **Liberar horário:** nada libera horário não confirmado hoje. Disponibilidade e agenda vêm do **Google Calendar**; liberar é apagar o evento (`cancel_event` do calendário dono). Mudar só o status deixa o horário ocupado. Com sinal Pix pago, liberar dentro da janela de reembolso **retém o dinheiro** (regras em `services/payments/deposit_lifecycle.py`).
- **Clínica vê:** `GET /tenants/me/calendar/events` (`api/hub/calendar.py::list_events`) **não devolve `status`**. `Brain-Message-Frontend` (agenda: `components/agenda/Drawer.tsx`, `lib/agenda/*`) tem o seletor de status, mas só mostra o que aprendeu de respostas de escrita; `secretarIA-frontend` (que vai virar site de marketing) tem o tipo de status mas fixa "agendado"; `brain-frontend` (`SecretariaPanel`) lista `/doctor/appointments` com selo "Confirmada".
- **Reaproveitáveis:** `send_cancellation_notice` + `services/cancellation_notice.py` (envio com botões, janela de 24 h, modelo, reentrega com `Retry`, escalonamento por e-mail); `enter_manage_action("reschedule", preselected_id=...)` e os passos de remarcar; `_begin_cancel`/`_manage_cancel`; `enter_rebooking`; `enter_decline_reasons` e `RebookingDecline`; `_load_upcoming_greeting_data`/`_adapt_greeting_has_upcoming` (já resolvem médico, serviço e `requirements`); `send_cancellation_escalation_alert` (e-mail operacional à clínica).
- **Limites de canal:** WhatsApp livre só dentro de 24 h da última mensagem do paciente (`send_buttons`, 3 botões, 20 caracteres por rótulo); fora, só **modelo aprovado pela Meta** (cobrado), com botões de resposta rápida de rótulos fixos; parâmetro de modelo não pode ter quebra de linha. O Portal não tem janela, modelos nem push: proativo só por e-mail.
- **O prompt da IA** (TASK-030, P5) proíbe prometer lembretes; o filtro de saída também. Quando este recurso existir, isso precisa ser ajustado.

## 4. Desenho

### 4.1 Cronograma de lembretes (dados)

Tabela nova e aditiva **`appointment_reminders`** — uma linha por lembrete planejado ou mostrado:

| Campo | Significado |
|---|---|
| `id`, `tenant_id`, `appointment_id`, `patient_id` | identidade; todo acesso por tenant e paciente |
| `kind` | `custom` (configurável), `day` (1 dia), `hour` (1 hora), `chat` (mensagem de abertura do chat) |
| `appointment_start_at` | o horário da consulta **quando a linha foi criada** (versão); remarcar invalida as linhas antigas |
| `due_at` | quando enviar (início − antecedência); `chat` nasce já enviado |
| `status` | `pending`, `sending`, `sent`, `failed`, `skipped`, `cancelled` |
| `channel` | `whatsapp` ou `email` (ou `chat`) |
| `with_prompt` | se a mensagem leva os botões de confirmar (falso depois de duas confirmações) |
| `attempts`, `last_error_code` | reenvio com limite; só código, sem texto do paciente |
| `sent_at`, `answered_at`, `answer` | `confirm`, `cancel` ou `other` |
| `warn_due_at`, `warned_at`, `warn_kind` | quando avisar a clínica (`unconfirmed` ou `delivery_failed`) |

Colunas novas e aditivas em **`appointments`**: `confirmation_count` (0–2), `first_confirmed_at`, `last_confirmed_at`. O `status` continua como está (`confirmed` passa a acompanhar `confirmation_count >= 1`; remarcar zera o contador e volta para `rescheduled`).

**Estado exibido (derivado):** `unconfirmed` (contador 0 e sem aviso), `confirmed` (1), `confirmed_twice` (2), `attention` (contador 0 e algum `warned_at` preenchido, consulta viva). Cancelada, compareceu e faltou mantêm suas cores.

**Regra de contagem:** cada confirmação de um lembrete/mensagem **diferente** conta uma vez, até o teto de 2; tocar de novo em Confirmar na mesma mensagem não conta. Confirmar manualmente pela equipe conta como uma.

### 4.2 O motor de lembretes

- **Criação:** em **todo caminho que cria consulta** (fluxo de botões, ferramenta da IA, promoção da reserva do Portal, criação pelo hub) — um ponto único `schedule_reminders(appointment)` chamado no mesmo lugar dos hooks de pós-agendamento. Cria as linhas `custom` (se a clínica configurou), `day` e `hour`; **pula** as que já estariam vencidas na criação. **Remarcar** (fluxo, botão, hub) marca as linhas antigas como `cancelled`, zera a confirmação e recria. **Cancelar/compareceu/faltou** cancela as pendentes. Migração/script para as consultas futuras já existentes.
- **Execução:** um cron novo `process_appointment_reminders` (a cada minuto) reivindica linhas vencidas com `UPDATE … WHERE status='pending' … RETURNING` (idempotente e à prova de duas cópias do worker), com tolerância máxima de atraso por tipo (ex.: o de 1 hora não sai depois de 30 min). Falha → até 4 tentativas com intervalo (padrão do `send_cancellation_notice`); esgotadas → `failed` + aviso de falha de entrega à clínica. Substitui `send_appointment_reminders` (a versão antiga fica desligada pelo interruptor da clínica, ligada para as demais até a migração completa).
- **Conteúdo:** "LEMBRE-SE: Sua consulta com {médico} está marcada para o dia {DD/MM/AAAA} às {HH:MM} para {serviço}." + requisitos do serviço (`requirements`) + botões **Confirmar / Cancelar / Outro**; para consulta de outra pessoa: "A consulta de {atendido} com {médico}…". Montado a partir do cadastro (médico, serviço, requisitos, atendido) por uma função única de texto.
- **WhatsApp:** dentro da janela de 24 h → mensagem com 3 botões (`send_buttons`). Fora → **modelo aprovado com 3 botões de resposta rápida** (nomes e textos entram no plano para submeter à Meta; requisitos achatados em uma linha por causa da regra de quebra de linha); enquanto o modelo novo não for aprovado, cai no modelo simples de hoje (sem botões) — e o paciente é conduzido pela mensagem de abertura do chat. As mensagens enviadas **passam a ser gravadas** como `Message` de saída (hoje não são), para aparecerem no console e no histórico da IA.
- **Portal:** e-mail transacional com o mesmo texto e o botão "Abrir minha conversa" (link do convite da clínica, obtido do brain-api); a mensagem de lembrete já fica gravada na conversa, esperando como primeira mensagem.
- **Identificadores dos botões:** ids novos `remconfirm|<reminder_id>`, `remcancel|<reminder_id>`, `remother|<reminder_id>` — a linha do lembrete dá tenant, paciente e consulta, então o toque é **escopado ao paciente** (o caminho antigo `apptconfirm|<appointment_id>` só confere o tenant). Os ids antigos continuam funcionando.
- **Parada:** com `confirmation_count >= 2` as linhas seguintes saem com `with_prompt=false` (lembrete simples) e **não geram aviso à clínica**.

### 4.3 A mensagem como primeira mensagem do chat

- **Quando:** o paciente escreve (texto livre) ou abre o chat com ao menos 6 h de silêncio (o mesmo piso da reativação) e tem **consulta viva no futuro**; mostra a mais próxima. Vem depois do portão de consentimento; substitui o menu genérico/oferta de continuar. Cria uma linha `chat`. Se `confirmation_count >= 2`: nenhum pedido de confirmação, só o menu normal. A mensagem do paciente é processada normalmente logo em seguida (se ela já traz um pedido claro, o fluxo/IA responde a ele), e as respostas genéricas de menu/saudação daquele turno são suprimidas porque o botão **Outro** já cobre isso.
- **Confirmar:** grava a confirmação (contador, `confirmed`), responde "Presença confirmada! ✅ Até {DD/MM às HH:MM}."
- **Cancelar:** cartão "O que você prefere?" com **Remarcar esta consulta / Marcar outra consulta / Não vou mais**:
  - **Remarcar:** passos de remarcação de hoje com a consulta pré-selecionada (a mesma consulta se move; zera confirmação, recria lembretes; limites do Pix valem).
  - **Marcar outra consulta:** abre uma reserva nova; a conversa guarda `flow_replaces_appointment_id` (coluna aditiva em `conversations`); a original é cancelada **somente quando a nova é confirmada** (no fim do caminho de confirmação, mesma transação do `Appointment` novo), e se o paciente desistir a original fica. Sinal Pix pago segue as regras de cancelamento de hoje.
  - **Não vou mais:** cartão de confirmar cancelamento → cancela (regras do Pix) → "por quê?" (`enter_decline_reasons`).
- **Outro:** entrega à IA (com a porta de volta ao fluxo da TASK-030).
- **Várias consultas:** a mais próxima primeiro; depois da resposta, uma linha "Você também tem consulta em {data}".
- **Portal:** os botões do lembrete precisam ser decodificados (hoje o Portal roteia toque pelo título); a decodificação valida o `reminder_id` contra o paciente da conversa.

### 4.4 Avisos à clínica, estado vermelho e ações na agenda

- **Aviso:** cada lembrete `kind` define `warn_due_at`: `custom` + 2 h, `day` + 2 h, `hour` + 20 min. Um cron `process_confirmation_warnings` envia o **e-mail à clínica** (endereço de alerta operacional já usado em `send_cancellation_escalation_alert`) se, naquela hora, `confirmation_count = 0`, a consulta está viva e o aviso ainda não saiu. No máximo três avisos por consulta; param ao confirmar, cancelar, liberar ou depois da consulta. Conteúdo: nome do paciente, médico, serviço, data e hora, qual lembrete, link para a consulta na agenda; **nenhum detalhe clínico**. Falha de entrega do lembrete gera um aviso diferente (`delivery_failed`).
- **Agenda (console da clínica, `Brain-Message-Frontend`):** passa a desenhar a partir dos **dados da consulta**: cores (neutro / verde ✓ / verde ✓✓ / vermelho), legenda, e na gaveta da consulta uma linha do tempo curta (lembrete enviado, respondido, clínica avisada). Eventos que só existem no Google (bloqueios pessoais) continuam como hoje.
- **Ações na consulta vermelha:**
  - **Liberar horário:** `POST /tenants/me/calendar/appointments/{id}/release` — apaga o evento do Google (calendário dono), marca `cancelled` com motivo `unconfirmed` e fonte `hub`, avisa o paciente com `send_cancellation_notice` (botões de remarcar), roda `deposit_lifecycle.on_appointment_cancelled`; **com sinal pago mostra o aviso de retenção antes e exige confirmação**.
  - **Enviar mensagem ao paciente:** texto livre pelo canal do paciente (WhatsApp fora da janela → modelo; Portal → mensagem no chat + e-mail avisando).
  - **Marcar como confirmado:** conta como uma confirmação.
- **API:** `GET /tenants/me/calendar/events` passa a devolver `status`, `confirmation_count`, `attention` e um resumo dos lembretes (aditivo; quem ignora chaves novas não quebra); `PATCH …/status` para `confirmed` passa pelo mesmo contador.
- **Sem liberação automática.** Os dados permitem acrescentar depois uma regra por clínica.

### 4.5 Configuração e liberação

- **Por clínica (colunas aditivas em `tenants`):** `reminders_v2_enabled` (padrão falso — interruptor da funcionalidade inteira), `reminder_extra_lead_minutes` (nulo = sem lembrete extra). O endereço de alerta é o que já existe. Tela de configuração da clínica ganha o campo (em `Brain-Message-Frontend`).
- **Ordem de deploy:** migração → API e worker juntos → front. Interruptor desligado em todas as clínicas; ligar primeiro na clínica de teste. Os modelos do WhatsApp são submetidos à Meta no início do plano 2 (a aprovação é externa).
- **Coordenação:** o plano 3 mexe nos mesmos arquivos de fluxo da TASK-030 (P2/P3) — rodar **depois** deles; conferir com o trabalho do Pix F2 (`feature/out-of-mvp`, TASK-017: reservas e vínculo da consulta).

## 5. Critérios de sucesso (verificáveis)

1. Consulta criada com folga suficiente gera as linhas certas (`custom` se configurado, `day`, `hour`); as vencidas na criação são puladas; remarcar invalida e recria; cancelar para as pendentes.
2. Cada lembrete sai uma única vez mesmo com dois workers; falha é reenviada até 4 vezes e depois avisa a clínica.
3. A mensagem tem médico, data **e horário**, serviço, requisitos e os três botões; consulta para outra pessoa nomeia o atendido.
4. Paciente do Portal recebe o e-mail com link e encontra a mensagem esperando no chat; paciente de WhatsApp recebe botões dentro da janela e modelo fora dela.
5. Toque de Confirmar só funciona para o paciente dono da consulta; confirmar duas vezes mostra ✓✓; confirmar a mesma mensagem duas vezes conta uma.
6. Depois de duas confirmações nenhum botão de confirmar é oferecido (lembretes simples) e nenhum aviso sai.
7. Paciente com consulta futura que escreve depois de 6 h de silêncio vê o lembrete primeiro; Cancelar leva a Remarcar / Marcar outra / Não vou mais; "outra" só cancela a original ao confirmar a nova.
8. Sem confirmação, a clínica recebe e-mail em +2 h, +2 h e +20 min; a agenda mostra vermelho; confirmar muda para verde na hora.
9. "Liberar horário" apaga o evento, cancela a consulta, avisa o paciente e respeita o Pix (aviso de retenção com sinal pago).
10. Nada acontece em clínica com o interruptor desligado.

## 6. Testes

Unitários do agendador de linhas (criação, pulos, remarcar, cancelar, backfill); reivindicação concorrente (duas execuções, uma envia); reentrega; texto do lembrete (com/sem requisitos, atendido, fuso da clínica, sem quebra de linha no modelo); decodificação dos botões novos e escopo por paciente (inclusive Portal); contador e teto de 2; mensagem de abertura (6 h, consulta viva, contador, várias consultas, supressão do menu); caminho do Cancelar nas três saídas e a regra de cancelar a original só ao confirmar a nova; avisos (3 momentos, parada por confirmação/cancelamento); liberar (evento do Google apagado, Pix com sinal pago, aviso ao paciente); isolamento de tenant em tudo; migrações em SQLite e em Postgres descartável; front: cores/✓/✓✓, gaveta, ações; avaliação com IA real de que "Outro" devolve ao fluxo.

## 7. Riscos do mapa → quem resolve

| Risco | Plano |
|---|---|
| Lembrete perdido quando o envio falha | R2 (linhas com tentativas) |
| Remarcar não gera novo lembrete de 24 h | R2 (linhas versionadas por horário) |
| Portal: marca gravada e erro a cada varredura | R2 |
| Toque de ação não escopado ao paciente | R2/R3 (ids por `reminder_id`) |
| `confirmed` sem data e sobrescrito | R1 |
| Horário liberado só no status deixa o Google ocupado | R4 (liberar apaga o evento) |
| Sinal Pix pago retido ao liberar | R4 (aviso e regras de hoje) |
| Dois lembretes (abertura do chat e cron) sem deduplicação | R3 (linha `chat` + contador) |
| Fuso (padrão UTC no lembrete, SP na saudação) | R2 (fuso da clínica em todo texto) |
| `reminder_opt_out` nunca é gravado (LGPD) | fora de escopo — seguimento recomendado |
| Modelo da Meta com aprovação externa | R2 (submeter cedo; fallback) |
| O prompt/filtro da IA proíbem falar de lembretes | seguimento no P5 da TASK-030 |

## 8. Fora de escopo

Liberação automática de horário; push do navegador/SMS; tela para o paciente desligar lembretes (e gravar `reminder_opt_out`); roteamento do aviso por médico; WhatsApp Flow; alterações no Pix F2; deploy (cada um só com pedido do dono).

## 9. Divisão em planos

| # | Plano | Depende de | Arquivos principais (dono) |
|---|---|---|---|
| R1 | Fundação: contador de confirmações, cronograma (`appointment_reminders`), colunas de configuração, `status`/confirmação na API da agenda | — | `models/appointment.py`, `models/appointment_reminder.py` (novo) + migração, `api/hub/calendar.py`, `schemas/calendar.py` |
| R2 | Motor de lembretes: agendador de linhas, cron, reenvio, texto, WhatsApp (modelo e botões), e-mail do Portal, ids de botão, parada após duas | R1 | `plugins/reminders.py`, `workers/` (novo cron), `services/reminder_text.py` (novo), `services/cancellation_notice.py`, `schemas/webhook.py`, `services/email.py` |
| R3 | Primeira mensagem no chat + caminho do Cancelar (Remarcar / Marcar outra / Não vou mais) + botões no Portal | R2 (e depois da TASK-030 P2/P3) | `workers/turn_router.py`, `workers/shared/greeting.py`, `workers/shared/actions.py`, `workers/portal/inbound.py`, `services/flow_router.py`, `models/conversation.py` + migração |
| R4 | Avisos por e-mail, estado vermelho, liberar horário e mensagem ao paciente (API) | R1, R2 | `workers/` (novo cron), `api/hub/calendar.py`, `services/email.py`, `services/payments/deposit_lifecycle.py` |
| R5 | Front: cores, ✓/✓✓, gaveta, ações e legenda na agenda; campo do lembrete extra na configuração da clínica | R1 (contrato) e R4 | `Brain-Message-Frontend` (`components/agenda`, `lib/agenda`, configuração) |

Ordem: R1 → R2 → R3 e R4 (R4 pode andar em paralelo com R3, arquivos diferentes) → R5 (pode começar contra o contrato do R1/R4). R2/R3 e R2/R4 tocam `services/email.py`/ids de botão: sequenciar onde houver arquivo comum (regra "nunca dois agentes no mesmo arquivo").

## 10. Referências

`plugins/reminders.py`; `workers/arq_worker.py`; `workers/turn_router.py::_route_inbound_turn`; `workers/shared/greeting.py`; `workers/shared/state_expiry.py::_reactivation_offer`; `workers/shared/actions.py::_handle_action_button`; `schemas/webhook.py::extract_action_button`; `services/patient_context.py`; `services/cancellation_notice.py`; `services/payments/deposit_lifecycle.py`; `services/flow_router.py` (`enter_manage_action`, `enter_rebooking`, `enter_decline_reasons`); `api/hub/calendar.py`; `models/appointment.py`, `models/rebooking_decline.py`; `Brain-Message-Frontend/components/agenda/Drawer.tsx`, `lib/agenda/*`; `docs/CHECKPOINT_appointment_status_taxonomy.md`, `CHECKPOINT_context_aware_opening.md`, `CHECKPOINT_pix_deposit.md`, `CHECKPOINT_brain_message_pipeline.md`; `docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md` (TASK-030).
