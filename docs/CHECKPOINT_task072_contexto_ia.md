# TASK-072 (contexto da IA) - nome/e-mail, perguntas em duas partes, recursos ligados/desligados

Origem: bateria real no Portal da clinica "Instituto Gicovate", 2026-10-29. Branch
`task/TASK-072-contexto` (base `main` 77b6017). **Local, sem push, sem deploy.** Deploy: API + worker
juntos (o worker monta o contexto; a API nao muda o comportamento).

Escopo: so o que a IA ouve (prompt/contexto) e uma ferramenta de nome. Os fluxos fixos (retencao de
horario, identidade do Portal) sao de outra frente da mesma task.

## O que entrou

| Problema | Onde | O que mudou |
|---|---|---|
| Nome/e-mail faltando | `services/llm_context.py::build_patient_identity`, `ai/prompts.py::_format_patient_identity` | Bloco "DADOS DO PACIENTE": so a situacao ("ja temos" / "ainda NAO temos"), nunca o valor (o prompt de sistema nao e pseudonimizado). A IA pede o que falta com naturalidade, uma coisa por vez. |
| Gravar o nome | `ai/tools.py::save_patient_name` (entra como ferramenta extra em `workers/shared/llm_context.py::_flow_handback_tools`) | So preenche `Patient.name` vazio (nunca troca), valida com `patient_name.parse_patient_name`, confere tenant e conversa, no Portal avisa o brain-api (`report_name`). Fora da edicao de consulta. Saida liberada na allowlist da v2: `saved`/`error`. |
| E-mail | (sem ferramenta, de proposito) | O e-mail do Portal e verificado por codigo no brain-api (dono da identidade) e o WhatsApp nao usa e-mail do paciente. Uma ferramenta gravando e-mail sem verificacao mandaria dados de consulta a um endereco nao provado. No Portal o bloco diz "ja temos" (o e-mail e provado antes da 1a mensagem); onde falta, a IA diz que a recepcao registra. |
| Perguntas em duas partes | `ai/prompts.py::_format_answering_rules` (v1 e v2) | Regra fixa "AO RESPONDER": responder todas as partes que o contexto permite na mesma resposta; so a parte desconhecida vai para a recepcao. |
| O que esta ligado | `services/llm_context.py::build_clinic_features`, `ai/prompts.py::_format_clinic_features` | Bloco "RECURSOS DESTA CLINICA": sinal Pix (LIGADO/DESLIGADO, de `Tenant.pix_deposit_enabled`), convenios (quantos cadastrados / so particular, do snapshot do fluxo), marcar-remarcar-cancelar pelo chat. Proibe mencionar recurso DESLIGADO (sinal desligado: nada de sinal, Pix, pagamento antecipado, "aguardando pagamento"). |
| "Reservado para outro paciente" | mesma regra "AO RESPONDER" + descricao de `get_availability` | Horario fora da lista simplesmente nao esta disponivel. A descricao da ferramenta deixou de dizer que "outros pacientes estao reservando". A frase "reservados para outro paciente, aguardando ... sinal" vem de `services/flow_router.py` (`HELD_SLOTS_NOTICE`/`ALL_HELD_NOTICE`): sai com a retirada da retencao (outra frente), nao foi tocada aqui. |

Os dois blocos de turno chegam por `run_agent(patient_identity=..., clinic_features=...)` (campos novos
de `TenantRuntimeConfig`, nunca lidos do banco em `load_tenant_config`). O orquestrador
(`workers/orchestrator.py`) so monta e passa; sem os campos o prompt fica sem os blocos.

## Prompt v1 mudou de bytes

A regra fixa "AO RESPONDER" entra tambem no prompt v1 (clinicas sem `ai_draft_v2`). Por isso foram
re-renderizados `_V1_DIGESTS` (`tests/test_prompts.py`) e `tests/golden/system_prompt_default.txt`
(+419 caracteres no caso "full"). Os blocos DADOS/RECURSOS so aparecem com os fatos do turno.

## Testes

`tests/test_prompts_task072.py` (blocos nos dois prompts, builders, `run_agent`, `save_patient_name`:
preenche, nao sobrescreve, recusa o que nao e nome, isolamento entre clinicas, aviso ao brain-api no
Portal). Ajustes: `tests/test_ai_toolset_v2.py` (`_HANDBACKS`), `tests/test_ai_v2_blindness.py` (chamada
roteirizada da nova ferramenta).

## Pendencias / pontos de atencao

- Conferir no Portal real (modelo real) que a IA pede o nome faltante e responde as duas partes numa
  so mensagem: o teste aqui e de montagem do prompt, nao do comportamento do modelo.
- Gravar e-mail por IA ficou de fora (ver acima); se o dono quiser, o caminho seguro e o fluxo de
  identidade (verificacao por codigo), nao a ferramenta.
- `orchestrator.py` ganhou ~12 linhas (variavel `patient_email`, tres kwargs em `run_agent`): se a frente
  do fluxo tambem editar esse arquivo, a mesclagem e trivial mas exige olhar.
