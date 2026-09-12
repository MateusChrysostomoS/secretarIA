# CHECKPOINT — toque real no Portal do paciente (canal Brain-Message) (2026-09-12)

Origem: `z_prompts/PROMPT_BRAIN_MESSAGE_PORTAL_INTERACTIVE_TAP.md` (raiz de BRAIN). Continuação de
`CHECKPOINT_interactive_bubbles.md` (decisão 3: "só o WhatsApp grava `interactive`") e de
`CHECKPOINT_inbound_tap_display_vs_routing.md` (título no `body`, id em `interactive_reply_id`).
Pares: `brain-api/docs/CHECKPOINT_portal_interactive_tap.md`,
`Brain-Message-Frontend/docs/CHECKPOINT_portal_interactive_tap.md`.

**Estado: BUILT, suíte completa verde (2092 passed; antes 2076), sem migração nova (as duas colunas
já existem e produção já está em `4c8e2a7f1b93`, `deploy_parity: match` em 2026-09-12), UNCOMMITTED,
não deployado.**

## §1 — O que mudou

| Onde | O quê |
|---|---|
| `services/channel_sender.py` | `BrainMessageSender.send_buttons`/`send_list` gravam `Message.interactive` via `interactive_buttons_record`/`interactive_list_record` (os MESMOS construtores do WhatsApp — mesmos cortes: 3 botões, 10 linhas, títulos truncados). `_record(body, interactive=)`. O `body` (histórico achatado da LLM) não mudou. |
| `schemas/internal.py` | `BrainMessageMessage.interactive` / `.interactive_reply_id` (opcionais, `None` por padrão, mesmo shape do `MessageRead` do console). `BrainMessageInbound.interactive_reply_id: str | None` (1..256 chars). |
| `api/internal.py` | `list_brain_message_messages` expõe os dois campos; `brain_message_inbound` repassa o id ao `enqueue_job`. |
| `workers/tasks.py` | `process_brain_message_inbound` e `_persist_brain_message_inbound` ganham `interactive_reply_id` e o passam a `_route_inbound_turn` — DEPOIS de `_validated_brain_message_reply_id` (§2). `offered_reply_ids` (pura) + `BRAIN_MESSAGE_TAP_WINDOW = 10`. |
| `schemas/conversation.py` | `interactive_read_or_none(blob, message_id=)`: o parser tolerante que era privado do hub, agora compartilhado pelo hub e pela rota do portal. |
| `core/whatsapp_limits.py` | `MAX_BUTTON_ID_CHARS = 256` (o literal que `interactive_buttons_record` usava) e `MAX_INTERACTIVE_REPLY_ID_CHARS = max(256, 200)` — o limite do campo novo é "o maior id que um cartão gravado pode conter". |
| `schemas/webhook.py` | **intocado** (só WhatsApp). |

## §2 — Validação do id (entrada não confiável)

No WhatsApp o id do toque chega num webhook ASSINADO pela Meta. No Portal ele vem do navegador da
paciente, via switchboard do brain-api (que garante QUEM fala, não o id). Regra implementada em
`_validated_brain_message_reply_id`, dentro da transação do turno, antes de `_route_inbound_turn`:

- o id só é honrado se estiver em `Message.interactive.options[].id` de um dos **últimos 10 cartões
  de saída da conversa DA PRÓPRIA paciente** (`(tenant, patient)` → `Conversation` → `Message`
  outbound com `interactive IS NOT NULL`, ordem `created_at desc`);
- qualquer outro id (forjado, de outra conversa da mesma clínica, ou de um cartão além da janela)
  vira `None`: o turno é roteado só pelo `text`, como mensagem digitada; log
  `brain_message_reply_id_rejected` (id truncado a 80 chars, `offered_count`);
- consequência: um `prof|<uuid de outra clínica>` forjado nunca chega a `inbound_routing_text`, então
  nunca vira `flow_selected_professional_id`.

Janela de 10 cartões: generosa para a paciente rolar pra cima, estreita para um cliente sem assinatura
injetar id de cartão antigo. O WhatsApp aceita toque em qualquer cartão antigo — a janela limita o
que um cliente NÃO assinado pode injetar, não o que o fluxo aceita.

## §3 — Decisões tomadas sem perguntar

1. **`greeting_button`/`action_button` NÃO são derivados do id no canal Brain-Message.** O prompt
   pedia só `interactive_reply_id` ("o resto do pipeline já sabe o que fazer"); `greeting_button` só
   muda algo para tenant com flows desligados (`flows_enabled` é sempre `True`) e `action_button` só
   nasce de lembrete (template HSM, que `BrainMessageSender.send_template` recusa). O toque no menu
   e no "✅ Concordo" roteia pelo TÍTULO, exatamente como no WhatsApp (`_is_consent_acceptance`,
   `_control_match`).
2. **Sem backfill** (mesma razão dos checkpoints irmãos): conversas antigas do Portal seguem texto.
3. **Limite 256** em vez de "maior caso real": nenhum id oferecido pode exceder o teto de gravação
   (`bid[:256]`, `rid[:200]`), então o limite é uma propriedade do código, não uma estimativa.
4. **Não criei a skill** sugerida em §3 do prompt ("validar id de controle tocado pelo cliente"): o
   padrão está documentado em §2 e no docstring de `_validated_brain_message_reply_id`; fica como
   pendência caso um terceiro canal apareça.

## §4 — Testes

- `tests/test_brain_message_interactive_tap.py` (novo, 16): sender grava o cartão igual ao WhatsApp
  (cortes incluídos) e texto não grava; `offered_reply_ids` tolera blob quebrado; a rota do portal
  serve `interactive` + `interactive_reply_id`; o endpoint enfileira o id (e `None` sem ele) e
  rejeita `""`/257 chars com 422; id oferecido chega ao router como `"<título> (<uuid>)"` e é
  gravado; id forjado (3 casos parametrizados), id de OUTRA conversa da mesma clínica e id de cartão
  além da janela → roteado como texto, `interactive_reply_id` NULL; **ponta a ponta** (worker real,
  `_send_bot_reply` real, `flow_router` real, sem rede): duas médicas com título de linha idêntico,
  toque na SEGUNDA → `flow_selected_professional_id` = segunda, sem LLM, próximo passo gravado como
  cartão `list`; controle: só o título não chega nela.
- `tests/test_interactive_bubble_record.py`: `test_brain_message_rows_stay_text_only` virou
  `test_brain_message_rows_record_the_card_too` (a decisão 3 do checkpoint irmão foi revertida de
  propósito).
- Suíte completa: **2092 passed**. `ruff check` limpo nos arquivos tocados; `ruff format` só no teste
  novo (`tasks.py` já estava fora de formato no HEAD — provado com `git stash`).

## §5 — Deploy (NÃO FEITO) — ordem obrigatória entre repos

Sem migração. Dentro deste repo: `secretaria_api` **e** `secretaria-worker` (o worker grava e valida,
a API serve e enfileira) — ordem interna indiferente, só campos opcionais. Entre repos:
**secretarIA → brain-api → Brain-Message-Frontend**. O frontend por último porque é quem PASSA a
mandar `interactive_reply_id`; brain-api antes dele porque `PatientMessageIn` é `extra="forbid"` —
frontend novo contra brain-api velho = 422 em todo toque ("Não foi entregue" na tela). Prova:
`GET /build` com `deploy_parity: match`; no Portal, uma conversa NOVA deve mostrar o aviso LGPD como
botão e a lista de profissionais como cartão; o toque deve aparecer no console de staff com ✓.

## §6 — Pendências

- Commit + `graphify update .` na hora do commit; deploy na ordem de §5, **com confirmação do usuário
  antes do brain-api** (nota do prompt, §0).
- Prova ao vivo em produção (§5 do prompt) — não feita: exige subir código não commitado nos três
  serviços, e o brain-api exige confirmação explícita.

## §7 — Prova ao vivo LOCAL (2026-09-12, depois do §6)

Feita com os TRÊS serviços reais encadeados por HTTP, fora de produção: secretarIA API (uvicorn
:8011) + worker arq real (Redis local) contra o Postgres descartável `bm-interactive-pg` (:5434, já
em `4c8e2a7f1b93`); brain-api real (uvicorn :8012, `SECRETARIA_BASE_URL` apontando para :8011,
chaves internas iguais) contra `brain-postgres` (:5433, `alembic upgrade head`); build de produção
do `Brain-Message-Frontend` servido por um harness Node de mesma origem que imita o `nginx.conf`
(`/api/brain/` → :8012). Clínica de teste `11111111-…` com duas profissionais de título de linha
idêntico ("🥼 Dra. Ana Beatriz Figu…"); paciente logada no Portal pelo fluxo de OTP real (código
gravado no banco local, SMTP desligado). Nada de LLM: todo o caminho é o fluxo determinístico.

Resultado no Chrome (`/conversa`): "oi" → aviso LGPD como botão "✅ Concordo" → toque → ✓ no botão,
"Opção escolhida" na resposta, menu "🗓️ Agendar"/"Outro" como botões → toque → lista "Ver
profissionais" como cartão → toque na SEGUNDA linha → no banco: inbound com
`interactive_reply_id = prof|8e2a47dc…` (a segunda linha do cartão gravado) e
`conversations.flow_selected_professional_id = 8e2a47dc…`, `flow_step = awaiting_service`; a
lista de serviços seguinte gravada como cartão `list`. Um driver HTTP (via brain-api, sem browser)
repetiu a mesma sequência numa conversa nova tocando a OUTRA gêmea (`c2aa2ece…`) e depois mandou um
id forjado `prof|<uuid aleatório>` com o mesmo título: o worker logou
`brain_message_reply_id_rejected … offered_count=8`, a linha inbound ficou com
`interactive_reply_id` NULL e o texto foi roteado normalmente.

Achado cosmético (não corrigido): na perspectiva da paciente, a legenda sob os botões e a nota da
folha da lista ainda dizem "Registra a resposta em nome da paciente" — texto da perspectiva do
balcão em `ReplyButtons`/`ListSheet`; fica como pendência de UI no frontend.
