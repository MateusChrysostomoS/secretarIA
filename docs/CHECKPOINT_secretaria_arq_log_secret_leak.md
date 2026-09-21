# CHECKPOINT — OTP / reset link leaking through arq's native job log

Origem: `TECH/BRAIN/z_prompts/PROMPT_SECRETARIA_ARQ_JOB_LOG_SECRET_LEAK.md` (Prioridade 0 de
`PLANO_PORTAL_COMO_WHATSAPP.md`). Executado 2026-09-21.

**Estado: BUILT, UNCOMMITTED, não deployado.** Suíte completa verde: 2385 passed (baseline 2377
+ 8 novos). `ruff check`/`ruff format --check` limpos nos dois arquivos tocados. Sem migração.

## 1. Causa raiz (reconfirmada na versão instalada, `arq==0.28.0`)

- `arq/worker.py::Worker.run_job` emite, para TODO job, em INFO, pelo logger stdlib
  `arq.worker`: `logger.info('%6.2fs → %s(%s)%s', …, ref, args_to_string(args, kwargs), extra)`.
- `arq/utils.py::args_to_string` faz `repr()` de cada argumento e **trunca em 80 chars**
  (`DEFAULT_CURTAIL`).
- `send_transactional_email(ctx, template, to, variables)` recebe o e-mail do destinatário
  (posicional) e `variables` — `{'code': <OTP>}` em `patient_access_otp`, `{'link': <URL com token
  de uso único>}` em `password_reset` (e no convite de equipe). A linha do arq saía assim:
  `send_transactional_email('patient_access_otp', '<endereço>', {'code': '<6 dígitos>', …})`.
- `core/logging.py::redact_secrets` nunca viu isso: é processor do **structlog**, redige por
  **chave** de `event_dict`; o arq usa `logging` puro com uma **string livre** já formatada.
- O mesmo logger também emite `← ref ● repr(result)` no fim do job (hoje `None` neste job, mas o
  filtro cobre o logger inteiro, não só a linha de início).

## 2. A correção

`core/logging.py`:
- `redact_free_text(text)` — mesmo vocabulário de `redact_secrets` (`_SECRET_HINTS` por
  substring, `_PII_KEYS` exato, sufixo `_encrypted`) + `_JOB_SECRET_KEYS = {"code", "otp",
  "link"}`, aplicado por regex a `'chave': valor` (dict repr) e `chave=valor` (kwarg repr). E-mail
  posicional (sem chave) é redigido pelo **formato** (`[\w.+-]+@[\w.-]*`).
- **Truncamento**: um valor sem aspas de fechamento (`'code': '918…`) termina em `$` — sem isso,
  o prefixo do código vazaria. Coberto por teste com endereço de 40 chars que faz o corte cair
  dentro do código.
- Valor que abre container (`'variables': {…}`) casa vazio, para a varredura entrar no dict.
- `ArqJobArgsRedactionFilter(logging.Filter)` reescreve `record.msg`/`record.args` só quando algo
  mudou; `install_arq_log_redaction()` (idempotente) instala em `arq.worker` e `arq.jobs`
  (os únicos loggers do arq que formatam dado de job — `arq.connections`/`arq.utils` só logam
  Redis). Chamado dentro de `setup_logging()`, que o worker roda em `arq_worker.py::on_startup`.
- O filtro fica no **logger**, não no handler: sobrevive ao `dictConfig` do CLI do arq
  (`disable_existing_loggers: False`, só configura o logger pai `arq`).

**Decisão de escopo: GENÉRICO**, não só `send_transactional_email`. Motivos: é o mesmo espírito do
`redact_secrets` (por chave, não por evento); o próximo job que carregar um segredo fica coberto
sem ninguém lembrar; e o custo é baixo (linhas sem chave sensível ficam byte-a-byte iguais —
teste `test_harmless_job_lines_are_left_untouched`). **Não** baixamos `arq.worker` para WARNING:
nome do job + duração é a única trilha operacional do worker.

**Limite conhecido:** segredo passado como argumento **posicional puro** (sem chave e sem formato
de e-mail) não é detectável num texto livre. Regra para jobs novos: segredo vai dentro de um dict
com chave nomeada (ou, melhor, não vai no job — passe uma referência).

## 3. Testes (`tests/test_arq_log_secret_redaction.py`)

Emitem a linha EXATA do arq com o `logger` e o `args_to_string` **do próprio arq** (truncamento
incluso), executam o job real `send_transactional_email` (só o envio SMTP é mockado) e capturam
tudo: `caplog` (stdlib), os eventos structlog do módulo `tasks` (gravados direto — structlog
cacheia loggers, `capsys` não os vê de forma confiável na suíte completa) e stdout/stderr.
Não-vacuosos: cada um afirma primeiro que a linha do job **foi** logada (nome do job, `template`,
`worker_transactional_email_processed`) e só então a ausência do OTP `918273`, do token
`rst-7c1e…`, do path `redefinir-senha` e do endereço. Mais: guarda de drift (falha se uma versão
futura do arq mudar o formato de `run_job`), truncamento, genérico em kwargs de outro job,
linha inócua intacta, e `setup_logging()` instalando o filtro uma vez só.

Controle negativo feito à mão: sem o filtro, a mesma linha contém o OTP sintético; com o filtro,
sai `('patient_access_otp', '***REDACTED***', {'code': '***REDACTED***'})`.

## 4. Outros loggers de terceiro (passo 3 do prompt)

`LOG_LEVEL` padrão é `INFO` (`config.py::Settings`). Checado:
- `smtplib` (envio de e-mail): não usa `logging`; só imprime com `set_debuglevel`, que o repo
  não chama. **Não se aplica.**
- `httpx`: loga `HTTP Request: METHOD URL` em INFO. Varredura por segredo em URL/query
  (`token=`, `key=`, `code=`, `params={…}`) em `src/secretaria`: nenhum — tokens vão em header ou
  corpo. **Não se aplica hoje**; se alguém puser segredo em query string, vaza por aqui.
- `arq.connections`/`arq.utils`: só estado do Redis (sem senha — o DSN não é logado).

## 5. Pendências

- **Deploy**: o log vaza no `secretaria-worker` (é lá que `run_job` roda). Exige deploy do
  WORKER; deployar só `secretaria_api` não corrige nada. Autorização explícita do dono pendente.
- Depois do deploy, confirmar com `get_service_logs` do worker que a linha de
  `send_transactional_email` aparece redigida — sem copiar linha nenhuma com dado real para
  fora da sessão.
- Logs **históricos** do worker (antes do deploy) continuam contendo OTPs e links: OTP expira
  em minutos; o link de `password_reset` expira em `ttl_minutes` do template (e o de convite em
  72 h). Nenhum valor real foi lido ou reproduzido nesta sessão.

## 6. Revisão de segurança (2026-09-21)

Reviewer independente (read-only) achou um bypass real: valor `bytes` (`{'code': b'918273'}`)
vazava, porque o prefixo `b` fazia o ramo "token nu" casar só o `b` e deixar a string entre
aspas intacta. Corrigido (o valor aceita prefixo `[bBrRuU]{0,2}` antes da aspa) + teste
`test_bytes_secret_values_are_redacted`. Verificado seguro pelo reviewer: OTP inteiro, dicts/listas
aninhados, repr com aspas duplas, aspas escapadas, a linha `● result`, truncamento no meio do
valor e da chave, ausência de ReDoS, e o filtro instalado antes do loop de jobs. Decisão: READY
após o fix. Suíte final: 2385 passed.
