# Lembrete com "Alterar Dados" — Design

**Data:** 2026-10-07 · **Task:** TASK-032 (R6) · **Status:** aguardando revisão do dono (spec escrita; plano só depois da aprovação)
**Origem:** teste em produção do R3 (2026-10-07): o dono decidiu trocar o caminho "Remarcar / Agendar Outra / Cancelar Consulta" por um único botão **Alterar Dados**, com um fluxo próprio para mudar data, horário, serviço, médico, convênio e paciente.
**Base:** R3 já no ar (`docs/CHECKPOINT_lembretes_r3.md`) e o mapa do código de 2026-10-07 (fluxo de gerenciar/remarcar, cartão de confirmação, modelo da consulta, limites dos canais). Funções citadas são âncoras estáveis, sem número de linha.

## 1. Objetivo e intenção

Hoje o lembrete tem **Confirmar / Cancelar / Outro**, e "Cancelar" abre um cartão de três opções. O dono quer algo mais direto e mais completo:

1. O lembrete e a abertura do chat passam a ter **Confirmar / Cancelar / Alterar Dados**.
2. **Cancelar** vai direto ao cancelamento (com "Tem certeza?", e depois pergunta o motivo). O cartão de três opções e os botões "Remarcar Consulta", "Agendar Outra" e "Cancelar Consulta" deixam de existir.
3. **Alterar Dados** abre um menu para mudar **data, horário, serviço, médico, convênio e paciente**, de forma que cada escolha respeita o que o paciente já tem (médico livre naquele horário, serviço do médico atual, convênio aceito pelo médico).
4. Toda mudança termina numa **mensagem de confirmação completa** (serviço, médico, dia, horário, convênio, paciente, orientações e o que mudou) com **Confirmar / Cancelar / Alterar Mais Dados**. **Nada muda na consulta de verdade até o paciente tocar Confirmar** nessa mensagem.
5. Quem toca **Confirmar** no lembrete recebe, em seguida, a **mensagem de paciente recorrente** (o cartão do menu principal: "Agendar" e "Outro").

Sucesso: o paciente consegue mudar qualquer dado da consulta sem falar com a clínica, nunca fica sem consulta por causa de uma falha, e a agenda do médico e o Pix continuam coerentes.

## 2. Decisões do dono (2026-10-07)

| # | Decisão |
|---|---|
| D1 | Lembrete: **Confirmar / Cancelar / Alterar Dados**. "Outro" sai desse cartão. |
| D2 | **Cancelar** substitui "Cancelar Consulta": confirma ("Tem certeza?"), cancela e **pergunta o motivo** (a lista de motivos que já existe). |
| D3 | **Confirmar** no lembrete → confirmação registrada + mensagem do **paciente recorrente** com os botões **Agendar** e **Outro**. |
| D4 | **Alterar Dados** abre: **Mudar data, Mudar horário, Mudar serviço, Mudar médico, Outro**. **Outro** mostra **Mudar convênio, Mudar paciente**. |
| D5 | **Portal:** botões de verdade (5 no menu). **WhatsApp:** a mesma escolha como **lista** (WhatsApp só aceita 3 botões). |
| D6 | Cada mudança termina na **mensagem de confirmação completa** com **Confirmar / Cancelar / Alterar Mais Dados**; "Alterar Mais Dados" reabre o mesmo menu. |
| D7 | **Mudar data:** lista os dias mais próximos do dia atual da consulta, pergunta "quer mudar o horário também?"; sim → lista de horários; não → confirmação. **Mudar horário:** lista de horários → confirmação. **Mudar serviço:** lista **só dos serviços do médico atual** → confirmação. **Mudar médico:** lista de médicos → confirmação. |
| D8 | **Lista de médicos** marca ✅ quem tem o dia e horário atual livres e ❌ quem não tem (como já se faz com o convênio no fluxo principal). **Convênio** marca quais planos o médico atual aceita. Tudo é "plugado" nos dados da consulta já marcada. |
| D9 | O texto do botão e a explicação de **Alterar Dados** são: "Alterar Dados: Mudar o horário, a data, serviço, médico ou qualquer outra coisa dessa sua consulta já marcada." |

## 3. Premissas minhas (a confirmar na revisão)

- **P1.** Escolher um médico marcado ❌ continua sozinho para a lista de dias e depois a de horários **daquele médico**, e só então vai à confirmação.
- **P2.** Mudar o **médico** troca o evento do Google para a agenda do novo médico: **cria o novo primeiro, grava no banco e só então apaga o antigo** (o paciente nunca fica sem evento).
- **P3.** Mudar o **horário** (ou data) zera o contador de confirmações e replaneja os lembretes (igual à remarcação de hoje). Mudar só serviço, médico, convênio ou paciente **mantém** o contador; os lembretes passam a mostrar os dados novos porque leem a consulta na hora de enviar.
- **P4.** Consulta com **sinal Pix pago**: o menu **esconde** Mudar serviço, médico e convênio (o valor pode mudar) e diz ao paciente para falar com a clínica. Mudar data/horário segue o limite de remarcações do Pix que já existe.
- **P5.** O paciente que digita texto livre no meio do fluxo continua indo para a IA, como hoje.
- **P6.** O botão do modelo de WhatsApp usado **fora da janela de 24 h** continua dizendo "Outro" até o dono reenviar o modelo à Meta (nunca foi aprovado). O cartão interativo (dentro da janela e no Portal) já passa a dizer "Alterar Dados". O texto novo vai para `docs/LEMBRETES_MODELOS_META.md`.

## 4. O que existe hoje e será reaproveitado

- **Seletores:** `enter_day_picker` e `_enter_slot_picker` (dias/horários), listas de médico, serviço e convênio (`_enter_professional_list`, `_service_list_bubble`, `_enter_insurance`), pergunta "pra quem" (`services/attendee.py`).
- **Cartão de confirmação:** `_recap_text` / `_confirmation_card`, com `Confirmar` / `Cancelar`.
- **Aplicação da remarcação:** `_apply_flow_result` (ramo `appointment_reschedule`), `reminder_hooks.after_appointment_rescheduled`, `deposit_lifecycle.register_reschedule`, limite do Pix em `workers/shared/deposit.py`.
- **Cancelamento + motivo:** `_execute_appointment_cancel` e `enter_decline_reasons` (já ligados ao "Sim, cancelar" do R3).
- **Substituição criar-depois-apagar:** `services/appointment_replacement.py` e `workers/shared/replacement.py` (modelo para trocar o evento de agenda).
- **Menu do paciente recorrente:** `menu_label(tenant)` + `main_menu_buttons()` (`[🗓️ Agendar, Outro]`).

## 5. O que muda

### 5.1 O lembrete e a abertura do chat
- `services/reminder_text.py`: `reminder_buttons` passa a **Confirmar / Cancelar / Alterar Dados** (`LABEL_OTHER` deixa de ser usado aqui; novo `LABEL_EDIT = "Alterar Dados"`, 13 caracteres). O texto padrão do lembrete ("LEMBRE-SE: …") **não** muda. A explicação de "Alterar Dados" (D9) aparece no **cartão do menu de mudança** (§5.3), como a explicação das três opções do R3 aparecia. **A confirmar (P7):** se o dono quiser a explicação também **no próprio lembrete**, ela entra logo abaixo do texto padrão, dentro do limite de 1024 caracteres do corpo.
- Id do botão novo: `remedit|<reminder_id>` (mesmo escopo por paciente e versão dos ids `rem*`). `remother`, `remresched`, `remnew`, `remgiveup` ficam **aceitos** por um tempo para cartões antigos ainda na tela (viram "Alterar Dados" ou "Cancelar", conforme o caso) e depois podem ser removidos.
- **Confirmar:** como hoje (`register_confirmation`), e **em seguida** envia a mensagem de paciente recorrente (menu principal com Agendar e Outro). O texto "Presença confirmada! ✅ Até …" continua na frente.
- **Cancelar (`remcancel`):** vai direto a "Tem certeza que quer cancelar a consulta de {quando}?" com **Sim, cancelar / Manter consulta** (com o aviso do Pix dentro da janela de reembolso). **Sim, cancelar** cancela (regras do Pix de hoje), fecha os lembretes e **pergunta o motivo** (`enter_decline_reasons`).
- Sai: o cartão de três opções, `CANCEL_PATH_TEXT`, `ACTION_BOOK_ANOTHER` (e o marcador `flow_replaces_appointment_id` deixa de ser escrito; a coluna e o consumo no fim da reserva ficam, sem uso, para uma limpeza futura).

### 5.2 O rascunho de mudança
- Nova coluna aditiva `conversations.flow_edit_draft` (JSON, nulo): `{"appointment_id", "service", "professional_id", "start_at", "end_at", "insurance", "attendee_name", "changed": [...]}` — só valores escolhidos; nome de terceiro apenas no campo `attendee_name` (mesma regra do `flow_draft`).
- Novo estado do fluxo `FlowState.EDIT_BOOKING` com passos próprios (menu, dia, "mudar horário também?", horário, serviço, médico, convênio, paciente, confirmação), saída por tempo (regra da skill `conversation-flow-state`: todo estado tem saída) e limpeza pelos pisos de silêncio.
- O rascunho **nunca** altera a consulta; só o **Confirmar** final aplica.

### 5.3 Os menus
- **Menu de mudança** (cartão com a explicação D9 e a lista do que cada opção faz): Portal = 5 botões; WhatsApp = lista de 5 linhas. "Outro" abre o segundo menu (**Mudar convênio, Mudar paciente, Voltar**).
- Portal com mais de 3 botões: `interactive_buttons_record` hoje corta em 3 (`MAX_BUTTONS_PER_MESSAGE`) nos dois canais. Passa a aceitar um teto maior **só quando o canal é o Portal**; o WhatsApp continua em 3 e usa lista.

### 5.4 Cada mudança
- **Mudar data:** dias mais próximos do dia atual da consulta (novo parâmetro "a partir de" no seletor de dias, hoje fixo em "a partir de agora, 20 dias") → pergunta "Quer mudar o horário também?" (**Sim / Não**) → lista de horários ou confirmação.
- **Mudar horário:** lista de horários do dia atual (`_enter_slot_picker`) → confirmação.
- **Mudar serviço:** só os serviços do médico atual (`professional_appointment_types`); se o novo serviço mudar a duração e o horário atual não couber, vai para dia/horário (como P1).
- **Mudar médico:** lista de médicos com ✅/❌ para o dia e horário atuais (consulta de disponibilidade por médico) e a marca de convênio aceito já existente (`_accepts_plan`); o serviço atual precisa existir no novo médico, senão a lista mostra os serviços dele para escolher.
- **Mudar convênio:** planos da clínica com a marca de quais o médico atual aceita; "Particular" e "Outro convênio" como no fluxo principal.
- **Mudar paciente:** pergunta "pra quem" e, para terceiro, a frase de autorização e o `ConsentEvent` (reaproveita `services/attendee.py`).

### 5.5 A mensagem de confirmação completa
Uma única função monta o texto (serviço, médico, dia e horário, convênio, paciente, orientações do serviço/da clínica quando houver, e uma linha "O que mudou: …"), com **Confirmar / Cancelar / Alterar Mais Dados**. **Cancelar** aqui descarta o rascunho e mantém a consulta como estava (não cancela a consulta), e diz isso. Limite de 1024 caracteres do corpo.

### 5.6 Aplicar o Confirmar
Uma operação só, na mesma transação do banco:
1. Revalida: a consulta ainda é do paciente, está viva e é futura; o horário ainda está livre; o limite do Pix (se mudou horário).
2. Agenda: mesmo médico = atualiza o evento (novo método `CalendarService.update_event_details` para título e descrição além de início e fim); médico diferente = cria o evento na agenda do novo médico **antes**, grava, e **depois** apaga o antigo.
3. Banco: atualiza a **mesma linha** (mesmo id): `appointment_type`, `professional_id`, `insurance`, `insurance_plan_id` / `insurance_professional_plan_id` (via `resolve_booking_plan_ids`), `attendee_name`, `start_at`/`end_at`, `google_event_id`/link; `status = RESCHEDULED` só se o horário mudou; `log_status_transition`.
4. Depois do commit: `after_appointment_rescheduled` quando o horário mudou; limpa o rascunho; envia a **mensagem de confirmação final** ("Pronto! Sua consulta foi atualizada: …") e o menu de paciente recorrente.
5. Falha no meio: nada muda para o paciente (o evento novo, se criado, é apagado); o rascunho fica para tentar de novo.

## 6. Canais
Tudo passa por `_reply_sender`/`_send_buttons_reply`/`_apply_flow_result` (skill `channel-aware-dispatch`). WhatsApp: lista para menus de 4+ opções; Portal: botões. Os ids dos toques são decodificados como os `rem*` do R3 (Portal só depois de provar que o id estava num cartão recente da conversa).

## 7. Segurança e coerência
- Todo acesso é escopado por clínica **e** paciente; um toque com id de lembrete de outro paciente é descartado (como no R3).
- Sem PII em log: só ids, motivos e contagens.
- Interruptor `Tenant.reminders_v2_enabled` continua mandando: desligado, nada disso aparece.
- Migração aditiva, nula, sem derrubar coluna (skill `frozen-contract-migration`); banco primeiro, depois API e worker juntos.

## 8. Testes (o que prende cada decisão)
Um teste por decisão D1–D9 e premissa P1–P5, incluindo: o rascunho nunca altera a consulta antes do Confirmar; Cancelar do cartão final mantém a consulta; falha na agenda não deixa o paciente sem evento; médico ❌ continua para dia/horário; Pix pago esconde serviço/médico/convênio; toque de outro paciente é descartado; WhatsApp recebe lista e o Portal recebe 5 botões; Confirmar do lembrete envia o menu recorrente; Cancelar pergunta o motivo.

## 9. Fora de escopo
Aprovação do modelo na Meta; remover a coluna `flow_replaces_appointment_id`; mudar a IA (TASK-030 P5); edição pelo hub/staff.

## 10. Riscos conhecidos
- Mudar médico mexe em duas agendas: coberto pela ordem criar → gravar → apagar.
- Disponibilidade por médico usa a agenda do Google em tempo real: lista com ✅/❌ pode ficar velha por segundos; a revalidação do §5.6 é a que vale.
- O teto de botões do Portal precisa ser conferido na tela do Portal antes do deploy.
