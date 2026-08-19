# CHECKPOINT — FIX 32: a chave de idempotência devolvida para um retry inexistente

> Estado: **BUILT + suíte verde, NÃO commitado, NÃO deployado** (2026-08-18).
> Escopo: `secretarIA` (backend) apenas. **Sem migração.**
> Suíte: **1671 passed, 1 failed** — a falha é o flake conhecido de UTC
> (`test_human_backup_plugin.py::test_on_inbound_inside_hours_returns_false`), confirmado
> pré-existente rodando o mesmo teste com a árvore limpa (`git stash`). Nenhum arquivo
> dessa correção é tocado por ele. `ruff check` limpo nos 10 arquivos alterados.

## O defeito

Dois módulos reivindicavam uma chave em `processed_events` antes de enviar e, quando o
envio falhava, **devolviam a chave "para que um retry possa tentar de novo"**. Esse retry
nunca acontecia:

- `workers/tasks.py::send_cancellation_notice` — `except: _release_event(key); log; return`.
- `plugins/professional_notification.py::_post_booking` — `if not sent: _release(...); log; return`.

O paciente não era avisado de que a consulta foi desmarcada; o médico não recebia o
e-mail do agendamento. Em ambos os casos o único vestígio era uma linha de log.

## A premissa do prompt estava errada, e isso mudou a solução

O `PROMPT_FIX_32` afirma que "arq só reexecuta um job que levanta exceção" e manda
**trocar o `return` por `raise`**. Isso **não funcionaria**.

Em `arq 0.28.0` (`arq/worker.py`, ramo de tratamento de exceção do `run_job`), só três
coisas fazem o job voltar para a fila: `arq.Retry`, `asyncio.CancelledError` e
`RetryJob`. Uma `Exception` qualquer cai no `else` final — `logger.exception(... failed)`,
`finish = True`, `jobs_failed += 1` — ou seja, **falha permanente, sem reexecução**.
`max_tries` (default 5) limita as tentativas pedidas via `Retry`; não transforma um
`raise` comum em retry.

Consequência prática: o comentário `# let arq retry` do job de transcrição
(`workers/tasks.py`, ramo `except TranscriptionError`) — citado no prompt como a
convenção correta a seguir — **também não reexecuta nada**. Está fora do escopo deste fix
e foi deixado como está de propósito (mexer nele muda o custo de re-transcrição), mas é
uma pendência real: ver "Pendências".

Portanto a Saída A foi implementada com `raise Retry(defer=...)`, não com `raise`.

## O que mudou

### 1. `services/email.py` — desligado não é falha

`send_transactional_email_message` devolvia `False` tanto para "EMAIL_ENABLED=false"
quanto para "o SMTP caiu agora". Quem quer decidir sobre retry precisa separar os dois:
nenhuma quantidade de tentativas transforma um kill-switch em e-mail entregue, e tratar
config como incidente alarmaria em todo agendamento de toda clínica sem e-mail.

- Novo `EmailOutcome` (StrEnum): `SENT`, `DISABLED`, `UNKNOWN_TEMPLATE`, `RENDER_FAILED`,
  `SEND_FAILED`, com a propriedade `is_transient` (só `SEND_FAILED`).
- Novo `send_transactional_email_result(...) -> EmailOutcome`.
- `send_transactional_email_message` virou **wrapper booleano** sobre ele — assinatura e
  semântica idênticas, então os call sites antigos (`workers/tasks.py`,
  `workers/onboarding_cron.py`) e seus testes não mudaram.
- Novo `send_cancellation_escalation_alert(to_email, clinic_name, whatsapp_link)`, no
  caminho de alerta operacional já existente (irmão de `send_calendar_alert` /
  `send_human_backup_alert`): fail-open, silencioso sem `SMTP_HOST`, e **nunca loga** o
  endereço nem o link.

### 2. `workers/tasks.py::send_cancellation_notice` — o retry passou a existir

O `except` agora devolve a chave, decide, e **levanta `arq.Retry(defer=...)`**. Bounds:

| constante | valor | por quê |
|---|---|---|
| `CANCEL_NOTICE_MAX_TRIES` | 4 | ≤ `max_tries` default do arq (5), para que o orçamento que estoura seja o **nosso** — com escalação — e não o do arq, que só loga "max retries exceeded" e descarta |
| `CANCEL_NOTICE_RETRY_DEFER_S` | 60 | cobre blip de rede / 5xx da Meta |
| `CANCEL_NOTICE_VALIDITY_S` | 900 (15 min) | aviso de cancelamento que chega horas depois alcança quem já saiu de casa — pior que nada |

A janela é medida a partir de `ctx["enqueue_time"]`, que o arq **preserva entre
retries**, e a decisão exige que a *próxima* tentativa também caiba na janela (retentar
aos 14m59s para entregar aos 16m não ajuda ninguém).

Esgotado o orçamento, `_escalate_cancellation_failure`:

1. loga `cancellation_notice_abandoned` com o campo estável
   `alarm="cancellation_notice_undelivered"` (sem telefone, sem link);
2. **e-mail para a clínica** (`tenant.contact_email`) com o `wa.me` que o hub já oferece,
   para o médico avisar o paciente em um toque.

`_emit_cancellation_usage` **saiu de dentro do `try`**. Antes, um soluço na métrica podia
alcançar o ramo de falha — que agora reenvia, e fora da janela de 24h isso seria uma
**segunda cobrança** pelo mesmo cancelamento. Agora é estruturalmente impossível.

### 3. `plugins/professional_notification.py` — o reenvio virou um job

Levantar de dentro do hook não adianta: `registry.run_post_booking` embrulha cada hook em
try/except **por contrato** (para um plugin quebrado não derrubar os outros), e esse
contrato não muda. Então:

- o corpo do envio virou `_deliver(tenant, patient, appointment)`, compartilhado entre o
  hook e o reenvio, **incluindo o claim** — é o que torna o reenvio seguro contra um
  agendamento que já recebeu e-mail;
- em falha transitória o hook **enfileira** `retry_professional_notification` e só
  **depois** devolve a chave (essa ordem é a correção: devolver antes e falhar ao
  enfileirar reproduziria o defeito);
- o job novo (registrado em `workers/arq_worker.py`) recarrega as linhas por id, chama o
  mesmo `_deliver` e **levanta `arq.Retry`** enquanto houver orçamento:
  `RETRY_MAX_TRIES=5`, `RETRY_DEFER_S=300`, `RETRY_VALIDITY_S=3600` — 5 tentativas, 5 min
  de intervalo, dentro de 1h. Mais generoso que o cancelamento de propósito: um médico
  lendo "nova consulta marcada" 40 min depois não perdeu nada;
- `_job_id` determinístico (`profnotifretry:<appointment_id>`) → no máximo **uma** cadeia
  de reenvio por agendamento;
- `PostBookingContext` ganhou o campo `redis` (default `None`), preenchido por
  `run_post_booking_hooks` — sem pool não há reenvio possível, e isso agora é dito em voz
  alta (`alarm="professional_notification_undelivered"`) em vez de assumido;
- `emails is None` (brain-api fora) passou a ser **transitório**, não no-op: antes o
  e-mail se perdia junto com "este médico não tem endereço", que é coisa diferente.

## Uma armadilha de teste encontrada no caminho

O teste de privacidade `test_no_phone_or_email_in_the_logs` usava `caplog` — que aqui é
**sempre vazio**, porque o structlog renderiza pela própria `PrintLoggerFactory` e não
passa pelo logging da stdlib. Ou seja: "nenhum telefone aparece no log" passava
**vacuamente**, inclusive contra código que logasse o telefone.

`capsys` também não serve sozinho: a suíte roda em `WARNING` com `ConsoleRenderer`
colorido, então linha de INFO nunca chega ao stdout e o marcador de nível vem embrulhado
em escapes ANSI — teste que passa sozinho falha na suíte inteira.

A solução usada é a que `tests/test_build_identity.py` já adotava: um `_LogRecorder` que
substitui `notif.logger` por monkeypatch e grava `(nível, evento, campos)`. Independente
de nível e de renderer, e com guarda de não-vazio em todo teste de ausência.

## Invariantes preservadas

- **Nunca envia duas vezes.** Todo caminho de reenvio passa pelo mesmo claim em
  `ProcessedEvent`. Testado nos dois módulos, inclusive na versão que custa dinheiro
  (template fora da janela de 24h: 1 envio, 1 evento de uso, mesmo com falha + retry).
- Contrato de contenção do `registry.run_post_booking` **inalterado** — há regressão
  cobrindo o caminho novo (falha transitória não impede `pix_deposit` no mesmo sweep).
- `_emit_cancellation_usage` segue fail-open, e agora fora do `try`.
- Nada de telefone, e-mail, justificativa ou conteúdo de conversa em log.

## Pendências

- **Commit + deploy.** Nada commitado. Isto toca `workers/` **e** `plugins/`: vale a regra
  do `CLAUDE.md` — deploy do `secretaria-worker`, não só do `secretaria_api`. O job novo
  `retry_professional_notification` só existe no worker; enquanto ele não subir, o enqueue
  falha e cai no ramo de alarme (fail-soft, nada quebra).
- **`FIX_15` (entrega durável de WhatsApp) ainda não executado.** Quando for, estes dois
  pontos são os clientes naturais do outbox: a tarefa vira **migrar** as duas chamadas,
  não criar um terceiro caminho de reenvio.
- **`# let arq retry` do `transcribe_audio_message`** continua mentindo (ver acima).
  Deixado fora do escopo por mexer no custo de re-transcrição — decidir explicitamente.
- **Escalação visível no hub.** Hoje a clínica é avisada por e-mail e o alarme fica no
  log. Expor "paciente NÃO avisado" na própria agenda exigiria coluna nova (migração), e
  foi deixado para quando o `FIX_15` definir onde o estado de entrega mora.
