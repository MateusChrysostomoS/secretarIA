# Índice de execução — Portal: mensagens, visita fundida, "digitando" (2026-10-01)

Origem: teste ao vivo (`docs/LACUNAS_PORTAL_2026-10-01.md`) + decisões do dono em 2026-10-01.
Cada plano é executável sozinho; este índice diz a ORDEM, o que roda em paralelo e quem é dono de cada arquivo.

## Decisões do dono incorporadas (2026-10-01)

- "Digitando" é UNIVERSAL, igual WhatsApp, e fixo: qualquer interação que esteja digitando aparece, e vale para qualquer produto futuro.
- "Paciente digitando" só é processado quando um humano da clínica conduz a conversa; com a automação conduzindo (secretarIA ou PreCheck) não se processa nada.
- Cadastro: o primeiro cadastro (apresentação, e-mail, LGPD e o que vier depois) fica como a conversa da pessoa; logins seguintes mostram essa conversa; cadastros repetidos são descartados. Sem proteção extra por "consulta marcada".

## Os planos

| # | Plano | Repos | O que entrega |
|---|---|---|---|
| A | `2026-10-01-portal-mensagens-recentes.md` | secretarIA (+ doc brain-api) | Conversa longa mostra as mensagens mais RECENTES (hoje congela nas 50 mais antigas). É a causa do "ficou sem resposta". |
| B | `2026-10-01-portal-visita-fundida-na-conta.md` | secretarIA, brain-api, front | Cadastro repetido não deixa uma conversa por cadastro; a visita é descartada e a pessoa cai na conversa antiga com o menu; apresentação só na primeira vez. |
| C | `2026-10-01-digitando-backend.md` | secretarIA + brain-api | "Digitando" universal, igual WhatsApp: automação, equipe da clínica e paciente (este só quando um humano conduz); contrato único para qualquer produto (campos na listagem, batimento do paciente, uma linha para registrar um produto novo). |
| D | `2026-10-01-digitando-frontend.md` | front | Bolha "digitando…" no Portal e no console, regra pura em `lib/typing.ts` (produto novo = uma entrada), teclado do paciente e da equipe avisando. |
| E | `2026-10-01-digitando-precheck.md` | PreCheck | Só a AUTOMAÇÃO do PreCheck digitando (paciente digitando não é processado no PreCheck, decisão do dono). Começa por uma exploração do código do PreCheck; mexe só no condutor do Portal. |

## Dependências

- A e C mexem na MESMA função (`list_brain_message_messages`) ⇒ **A antes de C**, na mesma branch/worktree da secretarIA.
- B (secretarIA) acrescenta rota e job em arquivos que C também toca (`api/internal.py`, `workers/tasks.py`) em pontos diferentes ⇒ worktree próprio; o merge é trivial (hunks distintos), mas **o Integrator resolve**.
- D depende do CONTRATO de C (campo `typing`, rota `/typing`), não do deploy: pode ser construído em paralelo, contra o contrato escrito. Só vai ao ar depois de C.
- B-front depende do contrato de B-brain-api (`superseded` já existe; o front só lê `patientRef`): independente em desenvolvimento, deploy por último.
- E: depois de C no ar (usa o contrato dele) e começa pela exploração (Task 0).
- Estrutura de workers pós-TASK-023 (já em `main`): o wrapper do turno é `workers/orchestrator.py`, o job de fusão nasce em `workers/portal/merge.py`, `workers/tasks.py` é só fachada, e os testes trocam nomes com `tests/_patching.py::workers_ns`. Os planos já usam esses caminhos.
- A deixa uma dependência do front: a rolagem do Portal e a poda de cópias locais descritas em `docs/CHECKPOINT_portal_mensagens_recentes.md` ("Dependência do front") entram em D (trilha T3) como a PRIMEIRA task — D já edita as mesmas dependências de efeito; até lá a prova ao vivo da Task 3 de A não mostra o que o paciente vê.

## Mapa de paralelismo (Dispatching Parallel Agents)

Três trilhas independentes de desenvolvimento, uma sessão/agente por worktree (regra de `AI_WORKFLOW.md`: worktree obrigatório com mais de um agente no mesmo repo; ownership de arquivo decidido ANTES):

| Trilha | Worktree (criar em `C:\TECH\BRAIN-worktrees\TASK-NNN\<repo>`, branch `task/TASK-NNN-<escopo>`) | Planos/tasks | Dono dos arquivos |
|---|---|---|---|
| T1 secretarIA-leitura + brain-api (digitando) | `TASK-024\secretarIA` e `TASK-024\brain-api` | A (tasks 1-2) depois C (tasks 1-6) | secretarIA: `api/internal.py` (listagem e rota `/typing`), `schemas/internal.py`, `services/typing_indicator.py`, `api/hub/conversations.py`, `schemas/conversation.py`, `workers/orchestrator.py` (só `_send_bot_reply`); brain-api: `services/message_switchboard.py` (`TYPING_PRODUCTS`, `send_typing`), rota `/threads/{product}/typing` e o contrato em `docs/PORTAL_MESSAGING_API.md` |
| T2 secretarIA-fusão + brain-api | `TASK-025\secretarIA` e `TASK-025\brain-api` | B (tasks 1-3) | `services/visit_merge.py`, rota `visits/merge` em `api/internal.py`, job `workers/portal/merge.py` + reexport em `workers/tasks.py` + registro no `arq_worker.py`; no brain-api `message_switchboard.merge_visit` e `complete_pending` |
| T3 front | `TASK-026\Brain-Message-Frontend` | D (todas) + B task 4 | `lib/typing.ts`, `components/console/*` (TypingBubble, Composer, ChatScreen, Thread), `components/patient/*`, `components/portal/*`, `lib/patient-portal.ts`, `lib/real/patient-access.ts`, `lib/console-api.ts`, `lib/real/console-api.real.ts`, `lib/mock/*` |

(T1 e T2 tocam o MESMO arquivo `message_switchboard.py` e o MESMO `patient_access.py` no brain-api, em funções diferentes: integrar T1 antes de T2 e conferir o merge.)

(Números TASK-024..026 são os próximos livres em `BRAIN/tasks/` em 2026-10-01; conferir antes de criar.)

Nunca dois agentes no mesmo arquivo. T1 e T2 tocam `api/internal.py` e `workers/tasks.py` em trechos diferentes — por isso worktrees separados e merge feito pelo Integrator, T1 primeiro.

## Papéis (mínimo suficiente — `AI_WORKFLOW.md`)

- **Implementer** por trilha (subagent-driven-development: um implementador novo por task + um revisor novo entre tasks).
- **Reviewer** independente no fim de cada trilha (diff, segurança — D e B tocam dados pessoais —, escopo de tenant).
- **Integrator** (a sessão principal): ordem de merge T1 → T2 → T3, SHAs, validação, `TASK.md` de cada trilha.
- Sem Explorer: o reconhecimento já foi feito (relatórios de 2026-10-01 estão nos próprios planos).

## Ordem de merge e de deploy

1. Merge: T1 → T2 → T3 (cada um em `main` do seu repo; fase MVP, não é a fase "fora do MVP").
2. Deploy (SÓ com autorização explícita do dono a cada etapa; nunca automático):
   1. **secretarIA: `secretaria_api` + `secretaria-worker`** (A, B e C juntos; sem migração). Conferir `GET /build` → `deploy_parity: match`.
   2. **brain-api** (B: chamada de fusão; C: só teste/doc).
   3. **frontend** (B e D).
3. Prova ao vivo em produção (cada plano tem a sua Task final): agent-browser, visitante novo + conta de QA; logs do worker só leitura (`get_service_logs`, sem env).

## Validação por repo (conforme `PRODUCTS.md`)

- secretarIA: `$env:BOT_ALLOWLIST_WA_IDS=""; uv run python -m pytest -q` (base hoje: 2778 passed, 10 skipped; falha pré-existente de fuso em `test_human_backup_plugin` entre 00-03h UTC não é regressão) + `uvx ruff check` nos arquivos tocados.
- brain-api: `uv run pytest` + `make lint`.
- front: `.\node_modules\.bin\tsc.cmd --noEmit` + `npm test` + `npm run build`.

## Fora do escopo desta rodada (registrado)

- Limite de mensagens por paciente no Portal (L6 das lacunas) — precisa de decisão de onde/quanto.
- A LLM prometendo o que não faz e faltando endereço/estacionamento/preço (L3-L5) — plano de prompt/contexto à parte.
- Console com várias pessoas da equipe: ver "outra pessoa da equipe digitando" (hoje a marca da equipe é por conversa).
- Falha antes do worker (brain-api responde `queued` mas nada é enfileirado) — nenhuma rede de segurança cobre; precisa de plano próprio.
