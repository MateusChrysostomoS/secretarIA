# CHECKPOINT — Sinal Pix ANTES de confirmar: hold, promoção por pagamento, itens de atenção (TASK-017, fase F2 da TASK F)

**Data:** 2026-09-29 (executado em 2026-09-30) · **Origem:** `Brain-Message-Frontend/docs/superpowers/specs/2026-09-29-task-f-sinal-pix-confirmacao-design.md`
(§17/§17A, fase F2) + plano `.../plans/2026-09-29-task-f2-pix-fluxo-hold-timing-atencao.md` ·
**Estado: BUILT no worktree `C:\TECH\BRAIN-worktrees\TASK-017\secretarIA` (branch
`task/TASK-017-pix-fluxo`, base `feature/out-of-mvp` e NÃO `main`: decisão do dono de 2026-09-30, a fase F
integra em `feature/out-of-mvp`), commits locais, NÃO pushado, NÃO deployado, migração `e2b7c4a9d1f5`
NÃO aplicada em banco de produção. Provas que dependem de credencial (sandbox Asaas, Google real, Meta): automáticas-ou-puladas, lista no §6.**

O que esta fase entrega: uma clínica escolhe (por clínica, em `deposit_timing`) entre `after` (a
consulta é confirmada e o Pix é pedido depois: o comportamento de sempre, o padrão) e `before` (o
horário fica SEGURADO durante a janela do Pix; a consulta, o evento do Google e o "consulta
confirmada" só nascem quando o pagamento é identificado). Nenhuma clínica muda de comportamento
sem o gestor da clínica trocar `deposit_timing` para `before` na configuração (o padrão é `after`).

## 1. O que entrou e onde

| Peça | Arquivo |
|---|---|
| Migração aditiva (`pix_deposits.conversation_id`, `expired_notice_sent_at`; `payment_attention_items.alerted_at`; a `paid_notice_sent_at` já vinha da F1, `c8a5d3f1b7e2`) | `migrations/versions/e2b7c4a9d1f5_deposit_before_timing.py` |
| Google: `create_event(event_id=)` idempotente (409 = sucesso) e `extra_busy` nas listas de horários | `services/calendar.py` |
| Decisão "esta reserva exige sinal?" (`quote_deposit`, `before_gate_active`) | `services/payments/deposit_gate.py` |
| Entrega neutra por canal (WhatsApp e Portal) e textos | `services/payments/notify.py`, `services/payments/deposit_lifecycle.py` (bloco `before`) |
| Hold em duas fases (código -> pagamento), trava por agenda no Postgres, novas saídas do `BookingGate` | `services/booking_hold.py` |
| Começar e abandonar a cobrança de uma reserva (`begin_payment`, `abandon_charge`) | `services/payments/booking_charge.py` |
| Estado `AWAITING_DEPOSIT`, envio do Pix, saída por tempo, taps "Já paguei / Trocar horário / Cancelar reserva", `/menu` que anula | `models/conversation.py`, `services/flow_router.py`, `services/deposit_wait.py`, `workers/tasks.py` |
| Dados do pagador (D7 generalizada: CPF/CNPJ do Asaas, e-mail do Mercado Pago): pedidos no chat só quando o provedor exige e só o que falta, nos DOIS modos, cifrados no Redis por até 15 min, resposta gravada como `[dado oculto]` | `services/payments/payer_document.py`, `payer_email.py`, `payer_data.py`, `payer_data_after.py`, `models/conversation.py` (`AWAITING_PAYER_DATA`), `services/booking_hold.py`, `services/flow_router.py`, `services/payments/deposit_lifecycle.py` (`maybe_create_deposit`, o `after`), `plugins/pix_deposit.py`, `workers/tasks.py` |
| Papéis no backend (TASK-020): só o gestor grava `payment_provider`/`deposit_timing`/`deposit_hold_ttl_minutes`; só gestor ou médico resolve um item de atenção; e-mail à clínica por item de consulta paga cancelada pelo paciente (um por item, sem repetição) | `api/hub/config.py`, `api/hub/payment_attention.py`, `services/payments/attention_alerts.py`, `services/email.py`, `workers/payments_cron.py` |
| Promoção idempotente (evento com id determinístico + `Appointment` + claim atômico) | `services/booking_promotion.py`, `services/payments/fulfillment.py` |
| Guardas: ferramentas de criação do LLM recusam, disponibilidade do LLM e do picker enxerga holds, `POST /appointments` e reagendamento do hub dão 409 | `ai/tools.py`, `plugins/multi_professional.py`, `plugins/multi_unit.py`, `api/hub/calendar.py` |
| Configuração: `payment_provider`, `deposit_timing`, `deposit_hold_ttl_minutes` + `GET /tenants/me/payment-settings` | `schemas/config.py`, `services/hub_configuration.py`, `services/payments/settings.py`, `api/hub/payment_settings.py` |
| "Pagamentos que precisam de decisão": `GET/POST /tenants/me/payment-attention-items`, e-mail à clínica, `needs_attention` no `deposit` da agenda, `GET /tenants/me/calendar/awaiting-payment` | `api/hub/payment_attention.py`, `services/payments/attention_view.py`, `services/payments/attention_alerts.py`, `api/hub/calendar.py` |

## 2. Contrato congelado ao fim da F2 (a F4 constrói sobre isto)

- `PUT /tenants/me/configuration` e `PUT /tenants/me/config` aceitam, dentro de `tenant`,
  `payment_provider` (`asaas|mercadopago`), `deposit_timing` (`before|after`) e
  `deposit_hold_ttl_minutes` (30-120). Recusa: `422 {"detail": {"code", "message"}}` com `code` em
  `deposit_disabled | addon_required | entitlement_unresolvable | account_not_ready |
  provider_unavailable | open_deposits`; o limite 30-120 é o `422` padrão do Pydantic. Um `PUT` que toque um desses três campos exige GESTOR: `403 {"detail": {"code": "forbidden_role" | "role_claim_missing", "message"}}` (secretária, médico sem flag de gestor ou token sem papel; nada é salvo, e o erro vem antes de qualquer validação). Um save que não toca os três campos segue aberto como antes.
- `GET /tenants/me/config` devolve os três campos. `GET /tenants/me/payment-settings` devolve o
  estado e o PORQUÊ dos controles desabilitados (`before_blockers`, `provider_switch_blockers`).
- `GET /tenants/me/payment-attention-items?status=open|resolved&limit&cursor` -> `{open_count,
  items[], next_cursor}`; `POST .../{id}/resolve` `{action, note, resolved_by_name}` -> `200` item
  resolvido, `403 {"detail": {"code": "forbidden_role" | "role_claim_missing", "message"}}` (só gestor
  ou médico resolve; secretária e token sem papel não; o `GET` segue aberto a todo papel do Chat), `404` (id de outra
  clínica), `409 already_resolved` (com o item), `422 {"detail": {"code"}}`, `429`. A F4 esconde "Resolver" para
  secretária, mas o backend é a regra.
- `GET /tenants/me/calendar/events`: `deposit` ganha `needs_attention: bool` (aditivo).
  `GET /tenants/me/calendar/awaiting-payment?start&end` -> `[{id, start, end, service,
  expires_at, professional_id}]`.
- `POST /tenants/me/calendar/appointments` e `.../reschedule`: `409
  {"detail": {"code": "slot_held_awaiting_payment", "message"}}`.

## 3. Decisões tomadas sem perguntar (com o porquê)

1. **Bloqueio de sinal obrigatório vira transferência para humano** (`action="handover"`): o spec
   pede "oferecer atendente"; oferecer um botão exigiria mais um estado. A conversa vai para a
   secretária e o evento fica no log (`booking_gate_deposit_blocked`). **Sem e-mail à clínica:** o plano previa o
   e-mail `deposit_required_failed` (debounce de 6 h), mas a F1, depois da revisão, só admite o e-mail de
   "consulta cancelada com sinal já pago" (resposta 13 do dono, Global Constraint 19); a execução seguiu a decisão
   posterior do dono e ajustou os testes das Tasks 6, 7, 9, 14 e 15 para `emails == []`.
2. **Expiração oferece o cartão que já existe** ("Sim / Menu principal", `LABEL_RETRY_*`) em vez de
   "Gerar novo Pix / Escolher outro horário": o mesmo horário pode ser escolhido de novo em um toque.
3. **`late_slot_taken` também cobre o conflito DENTRO da janela** (um evento que o médico digitou
   direto no Google): o texto ao gestor ficou "Pagamento recebido, mas o horário escolhido já não
   está livre." (o do spec diz "depois do prazo"). Não criei uma 11ª razão: o teste da F1 fixa o
   conjunto dos 10 motivos.
4. **O aviso por e-mail é uma varredura** (`alerted_at`), não uma chamada dentro de
   `open_attention_item`: assim cada chamador (evento, promotor, reconciliação, cancelamento) é
   coberto sem mudar a assinatura congelada da F1.
5. **Trava de agenda no Postgres** (`pg_advisory_xact_lock`) no `place_hold`: o teste de corrida não
   consegue FORÇAR o entrelaçamento, então a prova é determinística (a trava bloqueia a mesma agenda e
   não outra; mutação provada).
6. **A confirmação é enviada com claim atômico ANTES** (`paid_notice_sent_at`): duas execuções da
   promoção (job + varredura) nunca enviam duas vezes; um envio que falha solta o claim e é tentado de
   novo. Uma queda entre o claim e o envio perde o aviso (o compromisso já está na agenda).
7. **Desistência da promoção por idade do pagamento** (15 min, `GIVE_UP_AFTER`): não há coluna de
   tentativas; passado o prazo, a clínica recebe o item `promotion_failed` em vez de um laço eterno.
8. **`POST /blocks` do médico NÃO é restringido por holds** (urgência): a promoção revalida o Google
   e transforma o conflito em item de atenção.
9. **`resolve` com "devolvi no painel do provedor" só grava `cancelado_reembolsado`** para os motivos
   em que o sinal voltou inteiro por uma consulta que não existe (`late_slot_taken`,
   `paid_then_cancelled`, `refund_due_policy`, `promotion_failed`); para `amount_over` (o excesso) a
   consulta segue de pé e o dinheiro não muda de estado.
10. **Rate limit do `resolve` é fail-open** (sem Redis): resolver é idempotente e não move dinheiro.
11. **O `after` para paciente do Portal passou a entregar o Pix e os avisos** (antes falhava em
    silêncio: `wa_id=None`). Correção deliberada do modo `after`, provada por teste.
12. **D7 generalizada e dirigida pelo provedor.** Coleta o CPF/CNPJ (Asaas) ou o e-mail (Mercado Pago) só quando o
    provedor exige e só o campo que falta: `services/payments/payer_data.py` (`KIND_FOR_CODE` liga o código do
    provedor ao campo; um provedor novo acrescenta UMA linha). Não é uma regra por nome de provedor: se o sandbox
    mostrar que o `cpfCnpj` é opcional (veredito C4 da F1), a pergunta nunca acontece.
13. **A pergunta vem ANTES de segurar o horário no `before`** (um hold sem cobrança viva quebraria o invariante da D3) e
    a resposta refaz o "Confirmar" pelo caminho que já existe; no Portal o código do e-mail continua vindo
    primeiro (D12). **No `after`** (resposta 9 do dono) pergunta logo antes do Pix, pelo gancho `post_booking`; a
    consulta continua agendada durante a espera e a resposta retoma o Pix da consulta certa por um ponteiro no
    Redis (sem coluna, sem migração).
14. **Redação total das respostas em `AWAITING_PAYER_DATA`** (inclusive um `/menu` digitado naquele
    momento aparece como `[dado oculto]` no console de staff): uma regra por formato poderia perder um
    número mal digitado ou uma transcrição de áudio; a linha simplesmente não existe. O texto bruto que o arq guarda
    por 1 hora no Redis é aceito pelo dono (resposta 10) e nunca é mostrado ao LLM.
15. **O Portal é perguntado do e-mail como qualquer paciente** (Decisão tomada, padrão): o e-mail que ele verificou
    no login só chega aqui mascarado; ler o cru pede um contrato novo com o brain-api (pendência não bloqueante, §8).
16. **Papéis no backend pela TASK-020** (resposta 12): o `PUT` geral de configuração só exige gestor quando o patch
    toca um dos três campos de pagamento (`ensure_clinic_manager` com a claim de `get_hub_claim`), e o `resolve` usa
    `require_clinic_clinician`. O `GET` de itens e o diagnóstico `payment-settings` seguem abertos a todo papel do Chat
    (Decisão tomada, padrão: não expõem segredo). `pix_deposit_enabled` e `pix_deposit_percent` (campos que já
    existiam, cobertos por testes que não enviam papel) NÃO foram restringidos: entram em §8.
17. **E-mail à clínica só para `refund_due_policy`** (o item que o cancelamento do PACIENTE de uma consulta paga abre;
    resposta 13): um e-mail por item, com o item reivindicado de forma atômica ANTES do envio (`alerted_at`), sem
    debounce por clínica e sem Redis; um envio que não saiu devolve o item e é tentado de novo; `paid_then_cancelled`
    (a clínica cancelou, ou pagamento depois do cancelamento) e os outros motivos ficam só na tela.

18. **`PUT` de configuração sem `Depends(get_hub_claim)` obrigatório.** O plano ligava a claim como dependência dos dois
    `PUT`, o que fazia 50 testes antigos de configuração responderem 401; `api/hub/config.py` usa
    `_claim_if_authenticated` (None sem header; a claim real via `get_hub_claim` com header), e só os três campos de
    pagamento exigem a claim (sem header nesse caso: 401). O comportamento de produção é o mesmo, porque
    `get_current_tenant` já recusa quem não tem credencial.
19. **`paid_notice_sent_at` já existia** (F1): a migração `e2b7c4a9d1f5` acrescenta só `conversation_id`,
    `expired_notice_sent_at` e `alerted_at`.

## 4. Ordem de deploy (regra `frozen-contract-migration`)

0. brain-api com o papel no token do hub (TASK-020, parte 1) ANTES de qualquer rota com `require_clinic_*` ir ao ar: sem isso até o gestor recebe `role_claim_missing` ao salvar a configuração de pagamento (o recurso fica trancado, não vazado).
1. Migração `e2b7c4a9d1f5` (aditiva; `alembic upgrade head` pelo dono) — depois da `c8a5d3f1b7e2` da F1.
2. `secretaria_api` E `secretaria-worker` no MESMO commit (`GET /build` -> `deploy_parity=match`). O
   worker registra o cron novo (`run_attention_alerts`) e o hook de expiração; um worker atrasado não os
   tem (a varredura de promoção da F1 é a rede, não a solução).
3. Brain-Message-Frontend por último (F4): ele envia campos novos em `PUT /configuration` (o envelope
   recusa campo desconhecido) e lê os endpoints novos.
4. Nada de variável nova. `deposit_timing` continua `after` para toda clínica até o gestor da clínica trocar pela configuração.

## 5. O que NÃO foi feito (de propósito)

Aceitar CNPJ alfanumérico, ler o e-mail cru da conta do Portal (contrato novo com o brain-api), guardar cifrado o
e-mail já perguntado para não perguntar de novo, restringir `pix_deposit_enabled`/`pix_deposit_percent` ao gestor e
cobrir CNPJ no `pseudonymize-core` (tudo em §8). Também fora: o provedor Mercado Pago em si (F3), telas (F4),
estorno (D15), push/WhatsApp para o staff.

## 6. Prova

- **Resultado medido em 2026-09-30 (worktree `TASK-017`, com `PAYMENTS_TEST_PG_URL` apontando para um Postgres descartável): `3639 passed, 2 failed, 1 xfailed, 0 skipped` (baseline da Task 1: `3094 passed, 2 failed`); as 2 falhas são as mesmas do baseline (`test_action_buttons.py`, credenciais do Google Calendar). `alembic heads` = `e2b7c4a9d1f5` (cabeça única); `ruff check` limpo nos 77 arquivos `.py` tocados. Mutações de mordida provadas: papel do `resolve`, claim do e-mail antes do envio, filtro de motivos do e-mail, trava de agenda, e as 7 da coleta do dado do pagador (Task 14).**
- Local: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest` — só as 2 falhas pré-existentes de
  `test_action_buttons.py` (credenciais do Google Calendar) ficam vermelhas; `ruff check` limpo nos
  arquivos tocados; Postgres real (`PAYMENTS_TEST_PG_URL`): migração sobe/desce/sobe sem drift,
  8 trabalhadores promovendo a mesma cobrança criam UMA consulta/UM aviso/UM disparo de hooks, a
  janela expirando contra o evento pago nunca perde o pagamento, e a trava de agenda (com mutação).
- **Provas automáticas-ou-puladas (nunca uma parada):** com `ASAAS_SANDBOX_API_KEY` no ambiente, as provas do
  sandbox do Asaas rodam sozinhas e decidem se o `POST /customers` exige mesmo `cpfCnpj` (veredito C4 da F1), se ele
  aceita o documento que passou na validação local e o Pix ponta a ponta nos modos `before` e `after`; sem a variável o
  teste é `skipped` e o motivo é registrado aqui e no `TASK.md`.
- **Não provadas (dependem de credencial ou conta que só o dono tem; todas em §8):** a coleta do dado ponta a ponta
  no WhatsApp real e no Portal real (os testes usam remetentes e provedor falsos); o id de evento determinístico
  contra o Google real (409 em duplicata, `status: cancelled`); o envio fora da janela de 24 h do WhatsApp (o aviso
  tardio precisa de template aprovado, dependência externa); o ramo do e-mail contra o Mercado Pago real (não há conta
  de teste; o provedor fica construído e DESLIGADO no lançamento).

## 7. Pendências e riscos conhecidos

- (Fechada pela TASK-020) O papel agora chega ao hub e é imposto no backend (§3, item 16); o que sobra é a ordem de
  deploy do §4, item 0.
- Um `update_event` do reagendamento do fluxo do paciente não revalida holds entre a escolha e a
  confirmação (a lista de horários já os esconde).
- Os hooks de `post_booking` são disparados uma vez por quem CRIA a consulta; uma queda entre a
  criação e o disparo os perde (a consulta e o aviso não).
- Colunas Asaas antigas seguem até a fase "narrow" (migração futura com `source_fingerprint`).
- Dados do pagador (D7 generalizada): o provedor guarda o dado como o cliente/pagador dele (o paciente é avisado numa
  frase, aprovada pelo dono, sem portão jurídico); a exclusão desse cliente no Asaas é a do pedido de apagamento da
  F1 (`api/internal_privacy.py`, endpoint a provar no sandbox). O arq guarda o texto digitado no Redis pelo
  `keep_result` padrão (1 h), como toda mensagem e o código do e-mail: ACEITO pelo dono (resposta 10) e nunca mostrado
  ao LLM. O validador de documento é só numérico (CNPJ alfanumérico não é aceito). O `after` só pergunta com o chat em
  `IDLE`/`LLM`; em outro estado o Pix é pulado como sempre foi (log `payer_data_after_not_asked`).

## 8. Pendências não bloqueantes (ação do dono depois do merge)

Nada aqui trava a execução nem o merge; cada item é uma ação ou uma decisão de produto para DEPOIS.

1. Rodar as provas puladas por falta de credencial (§6): sandbox do Asaas (`ASAAS_SANDBOX_API_KEY`), Google real,
   janela de 24 h do WhatsApp e a coleta ponta a ponta nos canais reais.
2. Criar uma conta de teste do Mercado Pago e ligar o provedor (hoje construído e DESLIGADO) quando a F3 for ao ar;
   provar então o ramo do e-mail contra o Mercado Pago real.
3. Pedir ao brain-api um endpoint interno que entregue o e-mail verificado do paciente do Portal, para que o Portal
   não seja perguntado (§3, item 15); e, se quiser, guardar cifrado o e-mail já perguntado para não perguntá-lo de
   novo a cada Pix (a retenção é indiferente ao dono, resposta 10).
4. Restringir também `pix_deposit_enabled` e `pix_deposit_percent` ao gestor (hoje só os três campos novos são
   restritos, para não quebrar os testes e telas existentes).
5. Cobrir CNPJ (e revisar e-mail) nas regras de padrão do `pseudonymize-core` (`brain-shared-python-library`): rede
   de segurança para um valor digitado sem pergunta.
6. Aplicar a migração `e2b7c4a9d1f5`, fazer o deploy na ordem do §4 e conferir `GET /build` (`deploy_parity=match`).
7. Conferir em produção que os e-mails de "consulta cancelada com sinal já pago" chegam (SMTP configurado) e ajustar
   `attention_alerts.EMAIL_REASONS` se o dono quiser avisar outros motivos.

## 9. Revisão final independente (2026-09-30) e o que saiu dela

Um revisor de contexto novo leu a branch inteira (`8fda1b1..ea4fb41`) e devolveu 6 achados Importantes e 11 Menores. Os seis
Importantes foram corrigidos numa passada, cada um com um teste que falhou antes (RED -> GREEN) e a suíte inteira verde
(`3660 passed, 2 failed` = as 2 de baseline, `1 xfailed`):

| # | Defeito | Correção | Teste |
|---|---|---|---|
| I1 | Pago a tempo, mas a promoção demora além da janela: o horário voltava a ficar livre e outro paciente (sem ter pago) podia pegá-lo | O evento de pago (`event_processor._on_paid`, ramo `before`) segura o hold por mais 20 min (`PROMOTION_HOLD_MINUTES`), na mesma transação do "pago" | `test_a_paid_charge_keeps_its_slot_held_while_the_promotion_is_pending` |
| I2 | Com Asaas, "Já paguei"/reconciliação/expiração leem `paymentDate` (data sem hora = 00:00) e a primeira falha do Google virava definitiva | A janela de retry (`GIVE_UP_AFTER`) parte do MAIOR entre `paid_at` e o nosso carimbo `last_payment_check_at` (`fulfillment._promotion_started_at`) | `test_the_retry_window_starts_when_we_learn_of_the_payment_not_at_the_providers_date` |
| I3 | Consulta confirmada para um horário que já começou (Pix mais longo que o tempo até o horário, ou pagamento muito tardio) | `slot_conflict` devolve `"past"` (vira `late_slot_taken`, a clínica liga); `begin_payment` limita a janela ao tempo que falta e não reserva horário que começa em menos de 1 min | `test_a_payment_never_confirms_a_slot_that_has_already_started`, `test_the_hold_and_the_pix_window_end_at_the_slot_start`, `test_a_slot_that_starts_within_the_minute_is_not_reserved_for_a_pix` |
| I4 | Quem pagou no último segundo ouvia "O prazo do Pix acabou" (a saída preguiçosa não perguntava ao provedor) | O tratador da saída preguiçosa consulta o provedor (`_expired_but_paid`); pago -> volta a `AWAITING_DEPOSIT` e diz "pagamento encontrado" | `test_a_patient_who_paid_at_the_last_second_is_never_told_the_pix_expired`, `test_a_window_that_really_ended_still_ends_with_the_retry_card` |
| I5 | O spec 12.2 (R12) põe `pix_deposit_enabled` e a política de sinal como só-gestor; só 3 campos estavam protegidos (uma secretária podia desligar o sinal) | `PAYMENT_WRITE_FIELDS` ganhou os 6 campos (`pix_deposit_enabled`, `pix_deposit_percent`, `pix_refund_window_hours`, `pix_retention_policy`, `pix_partial_refund_percent`, `pix_reschedule_limit`) | `test_a_secretary_cannot_change_the_payment_fields_and_nothing_is_saved` (12 casos) |
| I6 | "A clínica foi avisada" era falso depois que o e-mail `deposit_required_failed` saiu (resposta 13) | A cópia diz o que acontece de verdade (a conversa vai para a equipe da clínica) | `test_the_blocked_copy_does_not_claim_an_alert_that_is_never_sent` |

O teste de `test_before_interplay.py` usava uma data fixa de julho de 2026 (já passada): o guarda de I3 o revelou e as datas foram
para 2030.

**Decisões que ficam para o dono (Ruling):**
- **Sinal obrigatório bloqueado não avisa a clínica** (I6): só há o log `booking_gate_deposit_blocked` e a conversa em humano. Uma causa sistêmica
  (chave do Asaas revogada, entitlement fora do ar) para TODAS as reservas de uma clínica `before` sem que ela saiba. A resposta 13 fala de
  e-mail; um sinal na tela (ex.: "agenda bloqueada: motivo X" em `GET /payment-settings`) não a contradiz e precisa de decisão de produto.
- **Teto da janela vs. piso do Mercado Pago (30 min):** a janela agora nunca passa do início da consulta; para um horário a menos de 30 min o Pix
  ficaria abaixo do piso do Mercado Pago. A F3 (provedor) deve recusar `before` nesses horários ou cair para `no_deposit`/bloqueio.

**Menores adiados (nenhum entrou na passada de correção):**
M1 promoção não toma a trava de agenda (corrida estreita entre dois pagadores tardios ou com `POST /appointments` do hub); M2 os hooks de `post_booking`
podem se perder numa queda entre o commit e o disparo; M3 evento fantasma no Google quando a promoção desiste; M4 o aviso "a clínica vai ligar" não é
reenviado; M5 conversa pode ficar em `AWAITING_DEPOSIT` com veredito "paid" após queda entre o claim e `leave_wait`; M6 a varredura de e-mail pode
passar fome com clínicas sem `contact_email` (e `SMTP_HOST` pode estar ausente em produção, o que deixa o único e-mail inerte); **M7 risco de deploy: os
estados novos `AWAITING_DEPOSIT`/`AWAITING_PAYER_DATA` quebram código anterior ao carregar a conversa (`flow_state` é enum não nativo): fazer o deploy da
API ANTES do worker e, num rollback, resetar esses dois estados para `IDLE`**; M8 o 422 do Pydantic vem antes do 403 de papel e um `null` explícito em
`deposit_timing` dá 500; M9 CNPJ alfanumérico; M10 o código Pix só é enviado uma vez; M11 desconectar a conta do provedor com cobrança aberta trava a troca
de provedor.

## Uma mensagem por Pix (2026-10-27, LOCAL - sem commit/push/deploy)
O aviso do Pix (sinal inicial `before`/`after`, diferenca da troca de servico pelo paciente e pela clinica) agora e UMA mensagem: texto + codigo copia-e-cola nas ultimas linhas, entre tres crases (`services/payments/pix_message.py::pix_code_tail`). WhatsApp desenha como monospace; o Portal reconhece a cerca e mostra a caixa cinza clicavel (Brain-Message-Frontend `lib/pix-code.ts`). Sem campo novo no contrato: mensagens antigas (sem cerca) seguem iguais. Pontos: `deposit_lifecycle.before_request_text(copy_paste=)` + `_deposit_request_text`, `service_change.difference_pix_text`, `workers/shared/deposit_request.py` (some a 2a bolha), `service_change_actions.service_change_bubbles`, `clinic_action_notice.notify_service_change` (some o envio `:code`). O cartao "aguardando o Pix" (botoes) continua separado. Testes: `tests/test_pix_single_message.py` + os que assumiam duas mensagens.
