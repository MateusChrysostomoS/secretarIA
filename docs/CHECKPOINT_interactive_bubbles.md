# CHECKPOINT — botões e listas como dado: `messages.interactive` (2026-09-11)

Origem: `z_prompts/PROMPT_BRAIN_MESSAGE_INTERACTIVE_BUBBLES_RENDERING.md` (raiz de BRAIN). Achado ao
vivo em 2026-09-10 no console de staff (tenant "Chrysostomo For Eyes", conversa `a939ed79…`): a lista
"Com qual profissional você gostaria de agendar?" aparecia como texto com "(opções: …)" e o botão
"✅ Concordo" do aviso LGPD não aparecia em lugar nenhum.

**Estado: BUILT, suíte completa verde (2076 passed; baseline do HEAD 2066), migração `4c8e2a7f1b93`
aplicada, revertida e reaplicada num Postgres local descartável (nunca em produção), UNCOMMITTED,
não deployado.** Par no frontend: `Brain-Message-Frontend/docs/CHECKPOINT_interactive_bubbles.md`.

## §1 — Causa raiz (confirmada no código)

- O caminho WhatsApp grava a mensagem de saída DEPOIS do envio (`workers/tasks.py::_record_outbound`,
  três chamadores: `_dispatch_bubbles`, `_send_greeting`, `_send_consent_notice`) e só gravava `body`.
- `body` é o contrato do histórico da LLM: `_bubble_history_body` achata lista/menu em
  `"<body>\n(opções: A, B)"`; saudação, aviso LGPD e cartão `[CONFIRM]` gravam o body puro — ali os
  botões sumiam por completo.
- `models/message.py::Message` não tinha coluna pra estrutura; `schemas/conversation.py::MessageRead`
  só tinha `body`.

## §2 — O que mudou

| Onde | O quê |
|---|---|
| `services/whatsapp.py` | `interactive_buttons_record` / `interactive_list_record` (puras): o cartão como o WhatsApp o desenha, com os MESMOS cortes (3 botões, 10 linhas, títulos/descrição/botão/seção truncados). `send_buttons`/`send_list` passaram a montar o payload A PARTIR desse registro — o que se grava e o que se entrega não têm como divergir. |
| `models/message.py` + migração `4c8e2a7f1b93` | `messages.interactive JSON NULL`, encadeada depois de `9d3b7e1f5a2c` (a coluna irmã `interactive_reply_id`). |
| `workers/tasks.py` | `_bubble_buttons`/`_slots_rows` (ids e linhas compartilhados entre envio e registro), `_bubble_interactive` (gêmeo de `_bubble_history_body`, que ficou intocado), `_record_outbound(..., interactive=)` nos três chamadores. |
| `schemas/conversation.py` | `InteractiveOption`, `InteractiveRead`; `MessageRead.interactive` e `MessageRead.interactive_reply_id` (opcionais, `None` por padrão). |
| `api/hub/conversations.py` | `_message_read_model` expõe os dois; `_interactive_read` é tolerante — um blob inválido perde só os controles daquela mensagem (loga `hub_message_interactive_invalid`), nunca derruba a thread. |

Forma gravada e servida:
`{"kind": "buttons"|"list", "body", "options": [{"id", "title", "description"}], "button_label", "section_title"}`
— `body` do cartão é o texto dele, SEM o "(opções: …)" que o `Message.body` guarda pra LLM.

## §3 — Decisões tomadas sem perguntar

1. **Vocabulário do fio em inglês (`buttons`/`list`), não `botoes`/`lista`** (o prompt sugeria como
   exemplo). O resto do fio é inglês (`inbound`, `bot`, `patient`) e traduzir pra UI é papel do mapper
   do console, que já traduz `bot → secretaria`.
2. **`JSON`, não `JSONB`**: convenção do repo (comentário em `models/conversation_pii_token_map.py`),
   a suíte roda em SQLite e nada consulta dentro do blob.
3. **Só o WhatsApp grava `interactive`.** O `BrainMessageSender` continua gravando texto
   (`interactive_history_body`) com `interactive` NULL: a paciente do portal RECEBE as opções como
   texto e digita. Gravar o cartão faria o console desenhar controles que ela nunca teve — e o console
   mostra o que a paciente viu. Corrige a premissa (f) do prompt: o portal `/conversa` não tinha o
   mesmo gap. Quando o portal ganhar toque real (pendência do dono), este sender passa a gravar.
4. **`interactive_reply_id` também vai pro fio** (coluna da sessão irmã): é como o console liga o
   toque ao cartão (✓ na opção escolhida + "Opção escolhida" na resposta). Dado de máquina, nunca
   exibido. Isso mudou um teste da sessão irmã: `test_the_staff_console_shows_the_title_and_never_the_id`
   afirmava o UUID ausente do JSON inteiro; agora afirma ausente de todo campo EXIBIDO (`body`,
   `interactive.body`, títulos, descrições) e presente em `interactive_reply_id`. Os ids das opções
   (`prof|<uuid>`, `slot|<iso>`) também viajam em `interactive.options[].id` — o mesmo dado que a Meta
   já recebe no payload, só pra staff autenticado do próprio tenant.
5. **Sem backfill**: linhas antigas seguem NULL e aparecem como texto. Reconstruir a partir de
   "(opções: …)" seria chute (rótulo pode ter vírgula, os ids nem estão lá) e editaria registro de
   conversa (LGPD).
6. **Export LGPD e `/internal/brain-message/*` inalterados** (`PrivacyMessage`, `BrainMessageMessage`):
   o portal não precisa (decisão 3) e o export é contrato consumido pelo brain-api (skill
   `frozen-contract-migration`).

## §4 — Testes

- `tests/test_interactive_bubble_record.py` (novo, 10): lista (`SlotsBubble`) grava as linhas E o
  body achatado fica igual byte a byte (literal fixado); `[CONFIRM]` + menu gravam botões com os ids
  enviados; texto não grava nada; aviso LGPD grava `consent|accept`/"✅ Concordo" com body inalterado;
  saudação com botões grava, sem botões não; Brain-Message fica só texto; `send_list` e `send_buttons`
  montam o payload a partir do registro, cortes incluídos; o hub serve cartão + id do toque; blob
  inválido não derruba a thread.
- `tests/test_inbound_tap_display_vs_routing.py`: ajuste descrito em §3.4.
- Suíte completa: **2076 passed**. `ruff check` limpo nos arquivos tocados; `ruff format` só no teste
  novo (nunca `ruff format .`).
- Migração num Postgres 16 descartável (Docker `bm-interactive-pg`, porta 5434): `upgrade head` →
  `downgrade 9d3b7e1f5a2c` (coluna some) → `upgrade head`; `alembic heads` = um só (`4c8e2a7f1b93`);
  DDL offline: `ALTER TABLE messages ADD COLUMN interactive JSON;`.

## §5 — Prova ao vivo (local, não produção)

Nada foi deployado (pedido do prompt: ficar uncommitted). A prova rodou com o código REAL de ponta a
ponta, fora de produção: Postgres descartável migrado; conversa gerada pelo caminho real do WhatsApp
(`_handle_patient_messages` com cliente WhatsApp falso — sem rede, sem LLM): "Oi" → aviso LGPD →
toque em "✅ Concordo" → menu → toque em "🗓️ Agendar" → lista de profissionais (4 linhas); API real
da secretarIA (uvicorn) + build de produção do `Brain-Message-Frontend`, servidos por um harness de
mesma origem que imita o nginx e responde a sessão de staff do brain-api (a única peça simulada —
identidade não é o que esta mudança toca). Achado útil pra próximas provas locais: o hub não valida
JWT localmente — `core/subscription.py::verify_subscription_token` chama o brain-api em
`/internal/secretaria/hub-token/verify`, e o harness responde esse callback.
Resultado no Chrome: aviso LGPD com o botão "✅ Concordo" marcado ✓, a resposta da paciente com
"Opção escolhida", o menu com "🗓️ Agendar" ✓ e "Outro", e a lista chegando como cartão com o botão
"Ver profissionais". Nenhum "(opções: …)" na tela. Scripts do harness e do seed ficaram fora do repo
(scratchpad da sessão).

## §6 — Deploy (NÃO FEITO) — ordem obrigatória

1. `alembic upgrade head` (one-off da imagem NOVA) com os dois serviços ainda no código velho — aplica
   também `9d3b7e1f5a2c` se ainda não estiver aplicada. SQL:
   `ALTER TABLE messages ADD COLUMN interactive JSON;`.
2. Deploy de `secretaria_api` **e** `secretaria-worker` (o worker grava, a API serve). Nunca código
   novo antes da coluna: o SQLAlchemy nomeia toda coluna mapeada em todo INSERT/SELECT de `messages`.
3. Depois o `Brain-Message-Frontend` (a ordem inversa também é segura: o mapper trata campo ausente
   como texto, e a API nova só acrescenta campos opcionais).
4. Prova: `GET /build` com `alembic_head: 4c8e2a7f1b93` e `deploy_parity: match`; uma conversa NOVA no
   WhatsApp deve mostrar o aviso LGPD e a lista como controles. Conversas antigas (inclusive a
   `a939ed79…`) seguem como texto — sem backfill.

Rollback: código velho nos DOIS serviços primeiro, depois `alembic downgrade 9d3b7e1f5a2c`.

## §7 — Pendências

- Commit (fica pra sessão de auditoria) + `graphify update .` na hora do commit (mesmo motivo do
  checkpoint irmão: `graphify-out/` versionado incharia o diff de revisão).
- Aba PreCheck do console: a transcrição do PreCheck não carrega as opções — correção é no PreCheck,
  descrita em `Brain-Message-Frontend/docs/CHECKPOINT_interactive_bubbles.md` §3.
- `answerInteractive` (staff responder em nome da paciente): decisão do dono, intocada.
