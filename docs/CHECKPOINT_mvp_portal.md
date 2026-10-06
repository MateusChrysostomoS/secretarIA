# Checkpoint — MVP da secretarIA no Portal

2026-09-30, TASK-021. Tasks 1–7B e bateria opt-in implementadas e validadas em código. Estado Git e SHAs no registro TASK-021 da workspace BRAIN. O usuário informou em 2026-10-01 que os serviços foram implantados; a prova no Portal consta em `tasks/TASK-021/results/agent_browser_production_2026-10-01.md` da workspace BRAIN. As correções TASK-022 abaixo são locais e ainda não foram implantadas por esta tarefa.

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

Testes locais cobrem rascunho/estado, retorno ao fluxo, PII, dedupe e falhas de envio/convite. A prova posterior em navegador confirmou o agendamento do paciente e o recebimento do e-mail segundo o usuário; a descrição do evento continha o convênio, mas o e-mail não mostrava seu nome. Descrição/lembretes do evento e agenda de staff não foram inspecionados diretamente nesta tarefa.

Claim no banco e envio externo não são transação única: crash após claim e antes de confirmação SMTP/Calendar pode deixar entrega ambígua ou suprimida. Replays normais/falhas parciais são cobertos; não há promessa de exactly-once em crash. Outbox/idempotência de provedor fica pendente.

## Implantação e ordem

Ordem original: **migração `patients.email` → brain-api → secretarIA API + worker (`secretaria_api` e `secretaria-worker`)**. Novo modelo lê coluna: subir código antes da migração quebra leituras de `Patient`. Coluna aditiva/nullable permite coexistência com código antigo; ausência do endpoint permite degradação controlada. O usuário informou que essa versão está implantada; esta tarefa não repetiu deploy nem SQL remoto.

## QA e correções locais de 2026-10-01 — TASK-022

- A QA no Portal percorreu `Outro` → pedido aberto → escolha de profissional → dia/horário → confirmação. Relato de sintoma testado pelo usuário expôs perguntas clínicas sucessivas sem avanço para consulta. O prompt agora orienta acolher brevemente sem anamnese informal e usar `set_booking_draft` para devolver o paciente às escolhas de agendamento, mesmo sem serviço/médico identificado. Sinais explícitos de urgência continuam com prioridade para a orientação de emergência existente.
- Os dois `Não sei` de profissional/serviço começam com orientação administrativa, não com pergunta sobre sintomas. O helper limitado passa a usar fatos do catálogo e perguntas de escolha; sua clarificação continua limitada a uma rodada antes de delegar ao agente geral. Testes cobrem o toque real, a primeira resposta e a transição seguinte.
- `set_booking_draft` sem serviço/médico abre a primeira escolha administrativa válida usando catálogo e profissionais atuais, sem consultar agenda ou marcar procedimento presumido. Convênio reconhecido atravessa a escolha do atendido e a autorização de terceiro, sem ser perguntado outra vez.
- Se um serviço já estava escolhido antes de voltar da LLM, a escolha posterior de profissional mantém esse serviço quando ele consta no catálogo do profissional; caso contrário, abre os serviços disponíveis desse profissional.
- Ambos os e-mails de confirmação mostram `Convênio: <nome>` quando o agendamento aponta para um plano cadastrado da própria clínica/profissional. Texto livre, nomes legados sem ID, referências inválidas e falha da consulta mantêm a frase genérica; não se usa o texto livre do paciente. E-mails já enviados não são reenviados automaticamente por causa da deduplicação.
- Validação final da mudança: suíte completa **2751 passed, 10 skipped, 2 failed**. As duas falhas são de credenciais Calendar ausentes no worktree isolado e já constavam no baseline anterior à tarefa; nenhuma falha nova permaneceu. Ruff completo e `git diff --check` passaram. Avaliação opt-in com modelo real segue não executada por ausência de `OPENAI_API_KEY`; a versão corrigida ainda não teve prova no Portal implantado.
- No Portal publicado, um toque em `menu` → `Escolher médico` recebeu HTTP 200, mas nenhum cartão apareceu em mais de 30 segundos. O endpoint apenas enfileira o turno; o primeiro cartão esperado seria `Essa consulta é pra você?`, antes de convênio e médicos. Sem resultado do job, registros de saída e estado de autorização do tenant, a causa desse episódio não ficou estabelecida.

## Escopo

Pix/sinal e Tasks 016/020 pertencem a `feature/out-of-mvp`. Envio automático pós-consulta e trabalho específico de WhatsApp ficam fora deste MVP. Decisão de escopo: add-on WhatsApp `plugins/human_backup` preservado, sem novo e-mail. Caminhos compartilhados existentes de transferência/confirmação mantêm seus canais; confirmação ao paciente/convite novos são do Portal. Implantação, credenciais e avaliação real continuam pendentes.

> TASK-030 P2: `set_booking_draft` ganhou a v2 (pra quem, dia, horário) atrás do interruptor `initial_flows.ai_draft_v2` e passa pelo resolvedor — ver `docs/CHECKPOINT_ia_rascunho_v2_resolvedor.md`.
