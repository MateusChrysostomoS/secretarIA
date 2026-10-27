# CHECKPOINT — Chat vendor switch: OpenAI or Claude (TASK-069)

Estado: **LOCAL, não commitado** — worktree `C:\TECH\BRAIN-worktrees\TASK-069\secretarIA`, branch
`task/TASK-069-claude-provider` a partir de `feature/out-of-mvp@fa7c5f4`. Sem deploy.

## O que entrou

- `ai/chat_model.py` (novo): a presença de `ANTHROPIC_API_KEY` decide (regra do dono, 2026-10-27: Claude Haiku em todos os pontos; OpenAI só se não houver chave da Anthropic; `LLM_PROVIDER` foi removido e é ignorado) o modelo de TODA chamada
  de chat — agente da conversa (`ai/graph.py` `build_agent`), decisão do scoped help
  (`ai/scoped_help.py` `_get_decision_model`) e leitura de fontes de contexto
  (`workers/shared/context_ingestion.py` `_structured_model`). Transcrição de áudio continua OpenAI/Groq.
- Config (`config.py`, `.env.example`): `ANTHROPIC_API_KEY`, `ANTHROPIC_THINKING` (`adaptive` | `disabled`, trazido da `main`),
  `ANTHROPIC_SECRETARIA_MODEL` (padrão `claude-haiku-5-5`, decisão do dono), `ANTHROPIC_MAX_TOKENS` (16000; o
  thinking conta no teto), `ANTHROPIC_EFFORT` (vazio = padrão do modelo, `medium` no Haiku 5.5).
- Reserva: sem `ANTHROPIC_API_KEY` mas com `OPENAI_API_KEY` → usa OpenAI e loga
  `llm_provider_fallback` (ERROR, uma vez por processo); sem nenhuma chave, falha com "ANTHROPIC_API_KEY missing".
- O texto fixo do sistema vai com marca de cache (`system_message`, trazido da `main`) quando o Claude responde.
- Diferenças absorvidas em `chat_model`:
  - saída estruturada: Claude usa `output_config.format` (`method="json_schema"`); o schema da
    extração de contexto é grande demais para isso (400 "Schema is too complex") e usa
    `native_schema=False` (ferramenta pedida, não forçada — forçar é 400 no Opus/Sonnet 5.5);
  - texto da resposta: Claude devolve blocos (`thinking`/`text`/`tool_use`) → `reply_text`;
  - `**negrito**` → `*negrito*` (`whatsapp_emphasis`, no `invoke_agent`);
  - recusa: `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`) nos modelos que aceitam;
  - retry de rede cobre os erros dos dois SDKs (`TRANSIENT_ERRORS`).
- `ai/trace.py`: entende `stop_reason` do Claude (`max_tokens`/`refusal` = turno falho).
- Dependência: `langchain-anthropic` (traz `anthropic`).
- `tests/llm_eval/*`: avaliações reais usam o fornecedor configurado (`eval_checks.real_chat_model`).

## Prova (2026-10-22, chaves locais, nunca exibidas)

- Testes afetados: 872 passed (antes do ajuste de schema) + 175 passed depois; `tests/test_chat_model.py` novo.
- Suíte completa (Haiku padrão, sem chaves no ambiente): **6.746 passed, 82 skipped, 1 xfailed, 0 failed** (base da
  feature: 6.730 passed; +16 testes novos; os mesmos 82 skips opt-in).
- Avaliações reais (`RUN_LLM_EVAL=1`, 2 rodadas): Claude Opus 5.5 42/49 e gpt-5-mini 42/49 no mesmo
  conjunto. Depois de corrigir 2 asserts que liam `content` cru, o bloco Portal-MVP do Claude ficou 9/10.
  Diferenças do Claude: em sintoma após "Outro" responde em texto em vez de abrir a seleção guiada; em
  especialidade inexistente não pergunta "quer agendar mesmo assim?"; checker de "consulta marcada" dá
  falso positivo em "não espere por uma consulta marcada". OpenAI falhou 1/2 na orientação de emergência
  (ofereceu humano em vez de 192/PS).
- Agente ponta a ponta (`graph.invoke_agent`, prompt v1): urgência → PS/192; endereço ausente → admite;
  4,8–9 s por turno.
- Fontes de contexto com Claude: 4 cenários sintéticos ok (conflito "different", injeção ignorada,
  texto sem relação só rascunhos), 13–36 s cada.

## Haiku 5.5 como padrão (2026-10-22, pedido do dono)

- Privacidade: a pseudonimização acontece antes da escolha do fornecedor (`scrub_messages` em `run_agent` /
  `scoped_help`; resultados de ferramenta mascarados no wrapper). Novo teste
  `test_the_request_body_sent_to_claude_is_masked` captura o corpo da requisição no transporte do
  ChatAnthropic: sem nome/telefone, só `[PACIENTE_…]`/`[TELEFONE_…]`. Thinking não é logado.
- Avaliações reais (2 rodadas): Haiku 5.5 **46/49**. Falhas: 2 falsos positivos do checker ("consultas já
  marcadas" = consultas existentes; preço real do catálogo citado ao admitir que não tem o da cirurgia) e a
  mesma diferença de comportamento (sintoma após "Outro" → texto em vez da seleção guiada).
- Agente ponta a ponta: preço via catálogo, `get_service_info` com loop de ferramenta, urgência → PS/192
  (3,3 s), negrito convertido.
- Fontes: 4/4 ok (11–18 s); conflito "different", injeção ignorada, texto sem relação só rascunhos.
- UI local (harness, Chrome): texto colado → "Pronto" em 11 s, endereço/cancelamento/acessibilidade
  preenchidos, diálogo de conflito do estacionamento com "Manter o meu" (screenshot em
  `tasks/TASK-069/s069-01-haiku-conflito-1280.png`).

## Pendências

- Porte para `main` (produção usa `main`) e commit.
- Produção: definir `ANTHROPIC_API_KEY` no painel, na API **e** no worker (Claude já é o padrão; feito pelo
  dono; automação não toca variáveis do painel). Sem a chave, o chat segue na OpenAI e o log acusa. Mudança de operador de dados (texto
  pseudonimizado passa a ir à Anthropic) — avaliar aviso de privacidade.
