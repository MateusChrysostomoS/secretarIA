# CHECKPOINT — observabilidade da LLM e "paciente nunca sem resposta" (2026-10-01)

Estado: **BUILT, uncommitted, não deployado.** Suíte completa: 2778 passed, 10 skipped. Exige deploy do
**worker** (toca `workers/tasks.py`); sem migração.

## Por quê

Teste do MVP (plano `Brain-Message-Frontend/docs/superpowers/plans/2026-09-30-mvp-secretaria-portal.md`)
mostrou silêncio do bot quando a LLM entra. A LLM era uma caixa-preta: nenhum log dizia o que o modelo
recebeu, quantas chamadas fez, que ferramentas usou, ou por que não devolveu texto.

## O que entrou

### 1. Rastreio do turno da LLM (`ai/trace.py`, `ai/graph.py`)

Eventos estruturados (nunca texto de paciente, nunca valores de argumento):

| Evento | Quando | Campos que respondem à pergunta |
|---|---|---|
| `llm_turn_started` | antes de chamar o modelo | `has_conversation_state`, `has_appointment_context`, `has_tenant_config`, `history_messages`, `history_chars` — o bloco de estado chegou ao prompt? |
| `llm_turn_trace` | depois de cada run do agente | `verdict`, `model_calls`, `tool_calls[{name,arg_keys}]`, `tool_errors`, `input_tokens`, `output_tokens`, `reasoning_tokens`, `finish_reasons`, `truncated`, `elapsed_ms` |
| `llm_turn_finished` | turno ok | `elapsed_ms`, `reply_len`, `is_sentinel` |
| `ai_run_agent_turn_timeout` | estourou `LLM_TURN_TIMEOUT_SECONDS` (120) | — |

`verdict` (ERROR quando ≠ `ok`, agrupável): `ok`, `truncated_no_text` (gpt-5 gastou todo o
`max_completion_tokens` raciocinando — hipótese mais provável de "resposta vazia"), `truncated_partial_text`,
`ended_on_tool_call`, `empty_model_output`, `no_text`.

**Captura de conteúdo (opt-in, `LLM_TRACE_CONTENT=true`)**: `llm_trace_prompt` (system prompt renderizado +
sha256), `llm_trace_history` (histórico JÁ pseudonimizado) e `llm_trace_reply` (resposta antes do rehydrate).
Desligado por padrão; ligar só durante a depuração e desligar depois.

### 2. Rede de segurança (`services/turn_safety_net.py`, `workers/tasks.py::_send_bot_reply`)

`_send_bot_reply` agora envolve `_send_bot_reply_inner`. Um contador por turno (ContextVar) é incrementado
nos dois pontos onde uma mensagem realmente sai (`WhatsAppClient._post`, `BrainMessageSender._record`). Turno
que terminou com 0 envios — por exceção OU por retorno silencioso (ex.: resposta que virou 0 balões) — recebe
UMA mensagem fixa, sem LLM (`TURN_FALLBACK_MESSAGE`).

Por que contador e não "linhas em `messages`": `_send_simple_text` envia no WhatsApp sem gravar linha; uma
contagem no banco chamaria esses turnos de silenciosos e duplicaria a mensagem.

Limites (anti-abuso): no máximo `TURN_FALLBACK_MAX_PER_WINDOW`=3 por conversa a cada
`TURN_FALLBACK_WINDOW_SECONDS`=600; depois disso fica calado e loga `turn_fallback_throttled`. Nunca responde a
tenant sem entitlement (silêncio intencional preservado). Eventos: `turn_failed`, `turn_unanswered`,
`turn_fallback_sent`, `turn_fallback_throttled`, `turn_fallback_skipped_not_entitled`.

`bot_reply_suppressed_unentitled` agora traz `entitlement_unknown` — "não consegui LER o entitlement"
(brain-api fora) deixou de parecer "tenant não paga".

`tests/conftest.py` neutraliza o envio da apologia nos testes antigos (clients falsos nunca passam pelos dois
pontos de envio reais); `tests/test_turn_safety_net.py` exercita a função real.

### 3. Orçamento de tempo

`run_agent` envolve o turno em `asyncio.wait_for(LLM_TURN_TIMEOUT_SECONDS=120)`. Antes: timeout HTTP 60 s × 5
retries do SDK × passos do ReAct podia passar dos 300 s do job do arq, que cancela o job sem resposta alguma.

## Como investigar um caso (roteiro)

1. Pegue o `conversation_id` e filtre os eventos acima no intervalo.
2. `turn_failed` / `turn_unanswered` → houve silêncio; `cause` diz exceção ou retorno silencioso.
3. `llm_turn_trace.verdict` ≠ `ok` → problema do modelo/contexto (truncamento, vazio, ferramenta).
4. `llm_activated.reason`: `router_delegated` (lacuna de fluxo) vs `sticky_llm_mode` (preso na LLM).
5. Se o conteúdo for necessário: `LLM_TRACE_CONTENT=true` no worker, reproduzir, ler `llm_trace_*`, desligar.

## Pendências / não feito

- Nenhuma mudança no **prompt** (`ai/prompts.py`): sem os logs acima não há evidência de qual regra confunde o
  `gpt-5-mini`. Suspeitas a confirmar: o prompt diz "acolher pacientes no WhatsApp" também no Portal; conflito
  entre "primeira mensagem: sem ferramentas" e o bloco de estado; `OPENAI_MAX_TOKENS=2500` com raciocínio.
- Rate limit de entrada só existe no webhook do WhatsApp (`_is_rate_limited`); o canal Portal não tem o
  equivalente neste worker (não verificado no brain-api).
- Falha de envio real continua sem retry (`_send_simple_text`, `_send_plain_reply` só logam); a apologia tenta
  de novo uma vez, mas se o canal está fora ela também falha (logado).
- Tenant sem entitlement continua sem resposta por desenho.
