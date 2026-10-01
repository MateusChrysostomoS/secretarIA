# Checkpoint — MVP da secretarIA no Portal

2026-09-30, TASK-021. Tasks 1–7B e bateria opt-in implementadas e validadas em código. Estado Git e SHAs no registro TASK-021 da workspace BRAIN. **Não deployadas.** Deployment: **NOT AUTHORIZED**. Sem SQL remoto ou alteração de produção.

## O que entrou onde

- `services/flow_router._delegate_llm_keeping_draft`: Outro/Não sei passam à LLM preservando serviço, profissional, convênio, dia e atendido; limpa etapa/horário para retomar com validação.
- `services/llm_context.build_conversation_state` e `ai/prompts._format_conversation_state`: etapa e escolhas no prompt; convênio aparece apenas como indicador e contagem, pois nomes legados não têm origem confiável para um bloco sem pseudonimização. Sem nome/e-mail/telefone do paciente ou nome do atendido. Consulta de terceiro é só indicador booleano.
- `ai/tools.set_booking_draft` e `workers/tasks._handle_set_booking_draft`: escolhas validadas retornam ao fluxo determinístico, dono do agendamento. Médico/serviço inexistente ou ambíguo dá erro recuperável.
- `ai/tools.request_human_handoff`, `workers/tasks._handle_human_handoff` e `services/handoff_notification.notify_human_handoff`: motivo enumerado e estado humano persistido antes de resposta/e-mail. Notifica clínica e profissional selecionado, deduplica e tenta três vezes inline. Falha final gera `human_handoff_notification_undelivered`. Conteúdo operacional usa ids/motivos, sem PII/texto do paciente.
- `services/calendar.build_event_description`: serviço, convênio (ou ausência explícita), atendido autorizado e canal no evento. Lembretes do dono da agenda: popup um dia/uma hora antes e e-mail um dia antes. `services/calendar.CalendarService.add_attendee` preserva convidados e envia convite com `sendUpdates=all`; paciente usa lembretes da própria agenda.
- `plugins/booking_notifications`: hook core `post_booking`, registrado no worker; confirmação operacional à clínica e, no Portal, confirmação ao paciente/convite Calendar. Resposta válida sem e-mail do brain-api limpa a cópia antiga; indisponibilidade permite a cópia guardada. Falhas de contato/envio/convite não desfazem agendamento. Claims independentes preservam sucessos no replay; falhas liberam o claim correspondente. Os novos e-mails indicam que há convênio sem reproduzir texto livre ou nomes; o valor exato permanece no agendamento e no evento. Sem job autônomo de retry.
- brain-api `api/portal/internal.patient_contact`: `POST /internal/brain-message/patient-contact`, autoridade de identidade, consumido por `services/brain_patients.fetch_patient_email`. Resposta válida atualiza cópia local; indisponibilidade usa cópia guardada ou segue sem e-mail/convite. Contrato em `brain-api/docs/PORTAL_MESSAGING_API.md`.

## E-mail e PII

**`patients.email` (`Patient.email`)**, nullable, guarda a cópia local. Não entra em `flow_*`, config/bloco do prompt, resultado de ferramenta ou logs. Histórico usa pseudonimizador canônico: uma linha `add_identifier("EMAIL", patient.email)` em `services/pii_pseudonymization.load_pseudonymizer`, sem código novo de máscara. Teste que espiona invocação do agente viu RED ao remover esse registro no bite check; registro restaurado. Limite conhecido: valores com menos de três caracteres não são registrados.

E-mail fica **em claro no banco, como `Patient.name`**; criptografia em repouso desses atributos não entra no MVP. Migração aditiva nullable teve smoke test local, sem banco remoto.

## Validação e limites

- secretarIA `uv run python -m pytest -q`: **2719 passed, 5 skipped, 3 failed**. Duas falhas de credenciais Google Calendar já existiam no baseline anterior às edições no mesmo worktree. Worktree sem `.env`; checkout principal tem arquivo, mas conteúdo não foi lido. A terceira é o teste de horário do add-on `human_backup`, reproduzido igualmente no `main` intocado e no worktree no mesmo horário. Essas falhas não provam regressões novas nem Calendar ao vivo.
- secretarIA `uv run ruff check src tests`: todos os checks passaram.
- brain-api, suíte completa: **1233 passed, 3 skipped**.
- `tests/llm_eval`: cinco cenários opt-in de primeira escolha de ferramenta. **Modelo real não executado**: `OPENAI_API_KEY` ausente; cinco skips no modo padrão. Nenhum `.env` lido. Com chave real no ambiente: `RUN_LLM_EVAL=1 uv run python -m pytest tests/llm_eval -v`.

Testes locais cobrem rascunho/estado, retorno ao fluxo, PII, dedupe e falhas de envio/convite. Sem prova em navegador, SMTP ou Calendar ao vivo; faltam implantação e credenciais adequadas.

Claim no banco e envio externo não são transação única: crash após claim e antes de confirmação SMTP/Calendar pode deixar entrega ambígua ou suprimida. Replays normais/falhas parciais são cobertos; não há promessa de exactly-once em crash. Outbox/idempotência de provedor fica pendente.

## Implantação pendente

**Migração `patients.email` → brain-api → secretarIA API + worker (`secretaria_api` e `secretaria-worker`).** Novo modelo lê coluna: subir código antes da migração quebra leituras de `Patient`. Coluna aditiva/nullable permite coexistência com código antigo; ausência do endpoint permite degradação controlada. Deploy **não autorizado**.

## Escopo

Pix/sinal e Tasks 016/020 pertencem a `feature/out-of-mvp`. Envio automático pós-consulta e trabalho específico de WhatsApp ficam fora deste MVP. Decisão de escopo: add-on WhatsApp `plugins/human_backup` preservado, sem novo e-mail. Caminhos compartilhados existentes de transferência/confirmação mantêm seus canais; confirmação ao paciente/convite novos são do Portal. Implantação, credenciais e avaliação real continuam pendentes.
