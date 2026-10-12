# CHECKPOINT — Lembretes R4: avisos, liberar horário e mensagem (TASK-032)

**Estado:** implementado e validado localmente conforme o plano `docs/superpowers/plans/2026-10-03-lembretes-r4-avisos-e-liberar.md`. **Commitado localmente** na branch `task/TASK-043-lembretes-r4-avisos` (worktree `C:\TECH\BRAIN-worktrees\TASK-043\secretarIA`, commits `f516ec3` (plano) .. `04de046` (correções da revisão final) + o commit deste checkpoint, sobre a `main` `4545933`). **Não** mesclado, **não** pushado, **não** deployado. Sem migração. Interruptor `reminders_v2_enabled` continua desligado em todas as clínicas.

Validação: baseline da suíte completa antes do R4 = 4530 passed, 68 skipped. Validação focada desta tarefa (arquivos do R4 + regressões listadas no plano): 109 + 139 + 101 = 349 passed; ruff limpo nos arquivos tocados. **Suíte completa (rodada após as correções da revisão final): `4643 passed, 68 skipped, 21 warnings in 511.63s (0:08:31)`** (baseline 4530 passed, 68 skipped; 0 failed).

## O que entrou onde

| Peça | Onde |
|---|---|
| Cron `process_confirmation_warnings` (a cada minuto): escolhe linhas vencidas, reivindica com `UPDATE ... WHERE warned_at IS NULL` e envia **um** e-mail por consulta | `workers/confirmation_warnings.py` (registrado em `workers/arq_worker.py`) |
| E-mail à clínica (`Tenant.contact_email`) e modelo `clinic_message_patient` | `services/email.py` |
| `POST /tenants/me/calendar/appointments/{id}/release` | `api/hub/calendar.py::release_appointment` |
| Aviso ao paciente depois de liberar (WhatsApp: job `send_cancellation_notice`; Portal: mensagem no chat + e-mail) | `services/appointment_release.py` |
| `POST /tenants/me/calendar/appointments/{id}/message` | `api/hub/calendar.py::message_patient`, `services/staff_patient_message.py` |
| Prévia do destino do sinal e texto de retenção para a clínica | `services/payments/deposit_lifecycle.py::preview_cancellation_outcome`, `release_warning_text` |
| Motivo `unconfirmed` (e campo `reason`) no log de transição | `services/appointment_status.py` |
| Schemas dos dois endpoints | `schemas/calendar.py` |

## Regras que não são óbvias

- `warned_at` é o marcador único: faz o aviso sair uma vez **e** deixa a agenda vermelha (`display_state == "attention"`, do R1). Ele é gravado antes do e-mail e nunca desfeito: sem endereço de alerta ou com SMTP fora do ar a agenda fica vermelha mesmo assim, e não há reenvio.
- Só linhas `sent`/`failed` geram aviso (contrato do R2). Confirmar, cancelar, liberar, encerrar ou o horário passar interrompe — inclusive entre a seleção e a reivindicação.
- Liberar **falha fechado**: se o Google recusar apagar o evento, 502 e nada muda (o delete é idempotente; 404/410 contam como sucesso). A exclusão acontece no calendário **dono** (o do profissional quando houver).
- Dois cliques/duas pessoas: o `UPDATE ... WHERE status IN (vivos)` tem um único vencedor; o perdedor recebe 409 `not_live` e não mexe no dinheiro nem avisa o paciente de novo.
- Dinheiro: `on_appointment_cancelled` tem exatamente três chamadas em `api/hub/calendar.py` (`cancel_appointment`, ramo `cancelled` do PATCH de status, `release_appointment`); no release é chamada uma única vez, depois do UPDATE guardado e antes do commit. O aviso ao paciente só sai depois do commit.
- Sinal Pix **pago** exige `acknowledge_retention=true` (409 `retention_ack_required` com o texto, antes de tocar no Google). Sinal não pago não exige.
- Consulta já confirmada exige `release_confirmed=true` (409 `already_confirmed`).
- Mensagem a paciente de WhatsApp fora das 24 h só com `notify_outside_window=true` (modelo `REMINDER_TEMPLATE_NAME`, uma variável, cobrado, evento de uso `reminders`); sem isso, 409 com o link `wa.me`. Paciente do Portal: mensagem no chat + e-mail genérico (nunca com o texto) se houver e-mail.
- As duas ações da equipe valem com o interruptor desligado (são decisões manuais da clínica); o interruptor só governa o que o sistema faz sozinho.
- Confirmação do paciente que commita durante a exclusão no Google **não** impede o release (deliberado: o evento já foi apagado). Sinal Pix pago durante a exclusão no Google pula o reconhecimento de retenção; o desfecho continua informado em `deposit_outcome`.
- Mensagem de WhatsApp a paciente sem conversa é enviada (e cobrada, se fora da janela) mas **não** fica registrada no histórico.
- Falha ao gravar a mensagem do Portal (envio ou commit) vira `DeliveryFailedError` (502 `delivery_failed`), com rollback e log só de ids e classe do erro. O 409 `not_live` do perdedor da corrida do release traz sempre `status`.
- Mandar mensagem **não** assume a conversa (não liga o atendimento humano).
- Depois de liberar, um rascunho aberto do R6 ("Alterar Dados") permanece na conversa; aplicá-lo é recusado e a consulta continua cancelada e sem edição. O rascunho some por tempo (expiração de estado), não pelo release.
- O log de transição agora carrega `reason`; o teste de taxonomia (`test_transition_log_is_sanitized`) foi atualizado para o novo campo.

## Decisões tomadas sem consulta

1. Falha do Google ao liberar = recusar (502), não "cancelar mesmo assim" (diferente do botão do paciente, que prioriza o pedido do paciente).
2. Aviso ao paciente do Portal sem botões de remarcar: o Portal ainda não decodifica `rebook*|` (R3); o texto manda escrever de volta.
3. Qualquer sinal pago exige reconhecimento, também quando o estorno seria total (o texto muda; a regra do plano/spec é "sinal pago").
4. Um e-mail por consulta por varredura, mesmo que várias linhas vençam juntas (queda do cron).
5. Link da agenda = `DOCTOR_AGENDA_URL` + `consulta=<id>` (sem configuração nova).

## Desvios na implementação (registro)

- A fixture de teste `seed_world` colide no `phone_number_id` único quando dois mundos são semeados; os testes de duas clínicas usam `pnid-2` (e `wa_id` distinto) no segundo mundo.
- Tarefa 8: `appointment.id` é lido para variável local antes do `rollback` (senão `MissingGreenlet` ao logar a linha expirada).
- Tarefa 6: teste do rascunho R6 escrito de verdade (aplica o R6 real após o release e confirma que a consulta segue cancelada e sem edição).
- Arquivos de teste novos carregam `# ruff: noqa: F811` no nível do arquivo (o `ruff format` moveu os `noqa` por função).

## Pendências

- **R5 (front):** botões Liberar / Mensagem / Marcar como confirmado na consulta vermelha; abrir a gaveta pelo parâmetro `consulta`; usar `cancel-preview` para o custo/janela; tratar os códigos 409/422/502 documentados no plano.
- `PATCH .../status` com `cancelled` continua **sem** apagar o evento do Google (lacuna antiga, fora do R4): a agenda deve usar `/release` (ou `/cancel`) para liberar horário.
- `POST /cancel` do hub ainda apaga no calendário do tenant, não no do profissional (mesma lacuna; `_owning_calendar` já existe e pode ser reaproveitado).
- O 422 "Google Calendar não conectado" continua com `detail` em texto simples (não objeto com código): o front precisa tratá-lo.
- A reivindicação do aviso à clínica não repete todas as guardas de seleção dentro do UPDATE; uma confirmação que commite durante a espera do lock da reivindicação pode deixar passar um aviso indevido (Postgres READ COMMITTED).
- A prévia do destino do sinal Pix pode diferir do resultado real se a fronteira da janela de estorno passar entre o 409 e o novo POST com reconhecimento; o 200 informa o que de fato aconteceu.
- Deploy (quando autorizado): ver o plano (seção "Deploy e liberação"). **API (`secretaria_api`) e worker (`secretaria-worker`) juntos** — o cron novo vive no worker; conferir `cron:process_confirmation_warnings` no log de registro do worker. Operador: `DOCTOR_AGENDA_URL`, SMTP e `contact_email` por clínica. Nada foi enviado, pushado ou publicado.
