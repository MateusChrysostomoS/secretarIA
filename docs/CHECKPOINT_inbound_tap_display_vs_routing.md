# CHECKPOINT — toque em lista: título pra exibir, id pra rotear (2026-09-11)

Origem: `z_prompts/PROMPT_BRAIN_MESSAGE_INBOUND_PAYLOAD_LEAK.md` (raiz de BRAIN). Achado ao vivo
em 2026-09-10 no console de staff: a resposta de uma paciente à lista "Com qual profissional você
gostaria de agendar?" aparecia como `"Dr. Fulano (8faa12e1-…)"` — o UUID interno da linha.

**Estado: BUILT, suíte completa verde (2066 passed), UNCOMMITTED, migração `9d3b7e1f5a2c` NÃO
aplicada em banco nenhum, não deployado.**

## §1 — Causa raiz (confirmada no código)

- `schemas/webhook.py::extract_inbound_body` montava **de propósito** `"<título> (<payload>)"` para
  as linhas da família `_PAYLOAD_ROW_PREFIXES` (`slot|`, `prof|`, `day|`, `daymore|`, `dayagain|`,
  `dayback|`).
- Um único caller em `src` (`workers/tasks.py::_handle_patient_messages`), e o valor ia sem
  transformação para dois lugares: `Message.body` (o que o console lê via
  `api/hub/conversations.py::_message_read_model`) e `_ReplyContext.inbound_body` (o que o router lê).
- Como o `flow_router` lê a escolha: **reparseando o texto** — `_professional_id_from_body`,
  `_slot_iso_from_body`, `_row_payload`/`_control_match`/`_day_from_body` tiram o `(…)` final por
  regex. O id bruto (`list_reply.id`) não chegava ao router por nenhum outro caminho.
- **Consumidor que o prompt não previa: o histórico da LLM.** `ai/graph.py::_load_history` (e
  `ai/scoped_help.py::_recent_history`) montam o histórico a partir de `Message.body`, e o system
  prompt (`ai/prompts.py`, bloco `[SLOTS]`) promete ao modelo que "o body que chega é
  `<rótulo> (<iso>)`". O agendamento pela LLM é `[SLOTS]` → toque → `[CONFIRM]` → "Confirmar" →
  `create_event`: o ISO do toque é lido do histórico **um turno ou mais depois**. Por isso a
  sugestão do prompt (§2, propagar o id só em memória até o router) não bastava — tiraria o ISO do
  histórico e quebraria o contrato do prompt da LLM. O id precisava ser persistido.

## §2 — O que mudou

| Onde | O quê |
|---|---|
| `schemas/webhook.py` | `extract_inbound_body` devolve só o título (texto digitado segue igual). Novas: `extract_inbound_reply_id` (id bruto do toque) e `inbound_routing_text(body, reply_id)` (recompõe, em memória, a string antiga). |
| `models/message.py` + migração `9d3b7e1f5a2c` | Coluna nova `messages.interactive_reply_id TEXT NULL` — a metade de máquina do toque. |
| `workers/tasks.py` | `_handle_patient_messages` extrai as duas metades; `_persist_inbound_message` e `_route_inbound_turn` ganham `interactive_reply_id` (default `None` — áudio, Brain-Message e os testes antigos não mudam). `_route_inbound_turn` grava título + id e decide sobre `inbound_routing_text(...)`: a escada inteira (LGPD, `/menu`, reativação, `route()`) lê a mesma string de antes. |
| `ai/graph.py`, `ai/scoped_help.py` | O histórico recompõe `título (payload)` a partir das duas colunas — a LLM vê byte a byte o que via antes. |
| `services/flow_router.py` | Só docstrings/comentários (quem produz o texto de roteamento). Nenhuma lógica. |

Invariante: `inbound_routing_text(extract_inbound_body(m), extract_inbound_reply_id(m))` é igual ao
que `extract_inbound_body(m)` devolvia antes, para toda mensagem (inclusive toque sem título).

Decisões:

1. **Coluna nova em vez de propagar o id só em memória** — §1 (consumidor durável).
2. **Toque sem título** (Meta sempre ecoa o título; caso degenerado): o body gravado vira `None` em
   vez do payload — gravar o payload ali reintroduziria o vazamento. O roteamento mantém o fallback
   antigo (payload para linha de dados, id inteiro para o resto). Divergência consciente da letra do
   prompt (§2.3, "ou `payload` quando não houver título"), alinhada ao goal da sessão ("nunca mais
   contém o id bruto").
3. **Sem backfill.** Linhas antigas continuam com `(uuid)` no body e `interactive_reply_id` NULL;
   `inbound_routing_text` é no-op para elas, então a LLM as lê como sempre. Reescrever corpo de
   mensagem é mexer em registro de conversa (LGPD) só para mudar tela antiga. A conversa
   `a939ed79…` segue mostrando o UUID nas mensagens já gravadas.
4. **Export LGPD (`schemas/privacy.py::PrivacyMessage`) inalterado** — o id é metadado de roteamento
   (o título já diz a escolha), e campo novo ali muda um contrato interno consumido pelo brain-api
   (skill `frozen-contract-migration`).
5. **O portal do paciente não era afetado por este bug.** `GET /internal/brain-message/.../messages`
   só serve pacientes `channel="brain_message"`, cujo inbound é texto puro
   (`BrainMessageInbound.text`) — o portal não produz toque com id. O vazamento era do console de
   staff (conversas WhatsApp).

## §3 — Testes

- `tests/test_inbound_tap_display_vs_routing.py` (novo, 12 testes, DB in-memory, entrada real
  `_handle_patient_messages`):
  - toque `prof|` grava só o título, `interactive_reply_id` = id, e o `_ReplyContext` ainda leva
    `título (uuid)`; os outros 5 prefixos idem (parametrizado);
  - o endpoint real do console (`GET /tenants/me/conversations/{id}/messages`) não contém o UUID em
    nenhum campo EXIBIDO (emenda 2026-09-11: desde `4c8e2a7f1b93` o id viaja como dado de máquina em
    `interactive_reply_id`, pra ligar o toque ao cartão — o teste passou a checar os campos exibidos;
    ver `CHECKPOINT_interactive_bubbles.md` §3.4);
  - **não-regressão do agendamento:** dois médicos com título de linha idêntico (nome acima do limite
    de 24); o toque no SEGUNDO passa por `_handle_patient_messages` → `_send_bot_reply` → `route()`
    → `_apply_flow_result` reais e a conversa persiste `flow_selected_professional_id` = segundo
    médico, sem chamar a LLM. Controle negativo: `route()` só com o título não chega nele;
  - `slot|` pelo router: toque real → `STEP_AWAITING_CONFIRMATION` com o ISO certo;
  - histórico da LLM (`_load_history`, `_recent_history`) ainda vê `🗓️ 15:00 (<iso>)`; linha antiga
    é lida sem "(…)" duplicado.
- `tests/test_webhook_parsing.py`: os 4 testes que fixavam o formato antigo no body agora fixam
  título no body + string antiga no texto de roteamento; tabela cobrindo TODOS os prefixos (com
  guarda que falha se um prefixo novo entrar sem teste), toque sem título, linha legada.
- Helpers `_tap`/`_day_tap`/`_control_tap` (`test_flow_router_multiprofessional.py`,
  `test_flow_day_picker.py`) e o toque manual em `test_agent_menu_tools.py` compõem pelo próprio
  `inbound_routing_text` — os testes de `route()` que já existiam passaram a exercitar a função real.
- **Prova de que morde** (plugins pytest descartáveis, sem editar código): (A) vazamento original
  restaurado → 9 falhas (todos os asserts de exibição); (B) "meia correção" que roteia só pelo
  título → 7 falhas, incluindo o teste ponta a ponta dos médicos gêmeos; (C) histórico da LLM sem
  payload → falha exatamente o teste do `[SLOTS]`.
- Suíte completa: **2066 passed**. `ruff check` limpo nos arquivos tocados; `ruff format --check`
  acusa 6 deles, todos já fora de formato no HEAD (provado nos blobs do HEAD) e nenhum trecho sobre
  linha desta mudança.
- `graphify update .` **não** rodado: `graphify-out/` é versionado (1199 arquivos) e encheria de
  arquivo gerado o diff que vai para revisão. Rodar na hora do commit.

## §4 — Deploy (NÃO FEITO) — ordem obrigatória

O SQLAlchemy nomeia toda coluna mapeada em todo INSERT (NULL explícito para coluna anulável sem
default) e todo SELECT de `messages`. Código novo contra schema velho = toda leitura/escrita de
`messages` falha — no worker, TODO turno de paciente.

1. `alembic upgrade head` (→ `9d3b7e1f5a2c`) a partir da imagem NOVA, como one-off, com os dois
   serviços ainda no código velho (a coluna é inerte para ele). SQL validado offline:
   `ALTER TABLE messages ADD COLUMN interactive_reply_id TEXT;`.
2. Deploy de `secretaria_api` **e** `secretaria-worker` (a mudança toca `workers/` e
   `flow_router.py` — regra do `CLAUDE.md`).
3. Sem one-off: API primeiro, migrar logo em seguida (até lá só a lista de mensagens do
   console/portal e o envio do staff falham), worker POR ÚLTIMO. Nunca o worker antes da coluna.
4. Prova: `GET /build` com `deploy_parity: match` e `alembic_head: 9d3b7e1f5a2c`; depois, um toque
   em "Escolher médico" numa conversa de teste deve aparecer no console só com o nome.

Rollback: `alembic downgrade aeeeb64360f5` + código velho. Simétrico no schema; as linhas gravadas
no intervalo ficam com body só título (a LLM do código velho perde o ISO desses toques no histórico —
nada é roteado errado: o router só lê o turno atual, que o código velho compõe sozinho).

## §5 — Pendências

- Deploy (§4) e prova ao vivo no console — não feita nesta sessão: exigiria subir código não
  commitado em produção.
- Commit (pedido: ficar uncommitted para revisão) + `graphify update .`.
- `PROMPT_BRAIN_MESSAGE_INTERACTIVE_BUBBLES_RENDERING.md` (irmão) — **executado 2026-09-11**
  (`CHECKPOINT_interactive_bubbles.md`, revisão `4c8e2a7f1b93` encadeada depois desta): acrescentou
  outra coluna em `messages` (estrutura de SAÍDA). Compatível com esta; se rodar depois, o rótulo da
  opção escolhida pela paciente é o `Message.body` limpo daqui.
