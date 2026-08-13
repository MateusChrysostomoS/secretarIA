# CHECKPOINT — Fluxos determinísticos incondicionais (fim do gate `initial_flows.enabled`)

Built 2026-08-13. Fecha a última porta pela qual uma clínica caía inteira na LLM sem
ninguém ter escolhido isso. Continuação direta de `CHECKPOINT_fixed_greeting_buttons.md`
e `CHECKPOINT_trio_gerenciar_scoped_help.md` (mesmo princípio de produto: **a LLM é o
último recurso, nunca o padrão**).

Suite state: **1117 passed** (`uv run python -m pytest -q`). `ruff check` limpo em todos
os arquivos tocados (os 7 achados do projeto são os mesmos pré-existentes que os
checkpoints anteriores já listam: `UP042` nos enums + `E501` em
`api/hub/__init__.py`, `config.py`, `test_ehr_plugin.py`, `test_post_booking_plugin.py`).

**Status: NÃO DEPLOYADO.** Código commitável, sem migração. Ver "Pendências".

## O problema

`flow_router.flows_enabled(tenant)` lia `tenant.initial_flows.get("enabled")`. Três fatos
que só juntos revelam o bug:

1. `Tenant.initial_flows` tem `server_default '{}'` (`models/tenant.py`).
2. O provisionamento (`services/provisioning.py`, e o bridge vindo do brain-api) **nunca**
   semeava a chave.
3. Nenhuma tela do hub escreve `initial_flows` — o form de configuração
   (`brain-frontend/app/(site)/secretaria/configuracao/`) não tem campo para ela; o
   `PUT /tenants/me/config` aceita o campo, mas nada no produto o envia.

Ou seja: **toda clínica nascia com os fluxos determinísticos desligados**, e só quem
soubesse chamar a API na mão ligaria.

O sintoma era enganoso, não silencioso. `workers/tasks.py::_greeting_buttons_for` manda o
trio fixo `[Agendar][Gerenciar consulta][Outro]` independente de `flows_enabled` (por
desenho, ver `CHECKPOINT_fixed_greeting_buttons.md` §2). Então o cartão de saudação
parecia certo — mas o toque em "Agendar"/"Gerenciar consulta" era interceptado pelo
short-circuit em `_persist_inbound_message` e degradava para o texto fixo
"entre em contato com a nossa equipe" (`_handle_greeting_button_unavailable`). Para quem
testava, o agendamento parecia quebrado; na verdade nunca tinha sido ligado.

## A decisão

O fluxo **não é escolha por clínica**. É o mesmo fluxo para todo mundo, e ele já se adapta
sozinho aos **dados** da clínica:

- 2+ profissionais ativos → `menu_buttons_for` troca o menu pelo trio multi-médico
  (`Escolher médico`/`Procurar médico`/`Outro`) e insere a etapa de escolha do médico;
- `collect_insurance` → insere a etapa de convênio;
- o catálogo de serviços dirige as opções da lista.

"Qual fluxo" é **derivado**, nunca configurado — então o gate não gateava mais nada.

`flows_enabled(tenant)` agora retorna `True` sempre. Mantida como função (em vez de apagar
os ~8 call sites) para que a decisão fluxo-vs-LLM continue tendo um único lugar nomeado.

`initial_flows` **não morreu**: `menu_label`, `buttons` e `reactivation` continuam sendo
lidos. Só a chave de liga/desliga saiu de circulação — inclusive um
`{"enabled": false}` explicitamente gravado, que não desliga mais nada.

## O que mudou

| Arquivo | Mudança |
| --- | --- |
| `services/flow_router.py` | `flows_enabled()` → `return True`, com o docstring que explica o porquê. |
| `models/conversation.py` | Comentário de `FlowState.IDLE` que ainda citava `initial_flows.enabled`. |
| `tests/test_flow_router.py` | `test_disabled_flows_delegate_llm` → `test_flows_run_even_when_never_configured` (guarda contra o gate voltar: `{}` e `{"enabled": False}`). |
| `tests/test_bot_reply_gating.py` | `_make_conversation` perdeu o kwarg `flows_enabled`; `_reply_context` ganhou `inbound_body` + `_LLM_ESCAPE`; o caso `..._when_flows_disabled` virou `test_unmatched_free_text_never_reaches_the_llm`. |
| `tests/test_action_buttons.py` | `_seed` perdeu `with_flows`; os dois casos de short-circuit viraram um parametrizado `test_greeting_button_is_never_short_circuited`; o polite-fallback do `apptresched` virou `test_apptresched_enters_manage_flow_on_an_unconfigured_tenant`. |
| `tests/test_patient_context.py` | O caso flows-disabled agora afirma que o trio `[Remarcar][Cancelar][Outro]` vence numa clínica não configurada. |

Sem migração: nenhuma linha de `tenants` precisa ser reescrita, justamente porque o valor
armazenado deixou de ser lido.

## Código morto que ESTE round deliberadamente não removeu

O único produtor de `_ReplyContext.greeting_button_unavailable` era a condição
`not flows_enabled(tenant)` em `_persist_inbound_message`. Com o gate incondicional, todo
esse caminho de degradação é **inalcançável**:

- o bloco `if greeting_button is not None and ... and not flows_enabled(tenant)` em
  `workers/tasks.py::_persist_inbound_message`;
- o campo `_ReplyContext.greeting_button_unavailable` e o seu consumo em `_send_bot_reply`;
- `_handle_greeting_button_unavailable` e os seus textos fixos;
- o ramo equivalente em `_handle_action_button` (`apptresched` sem fluxo).

Não foi removido porque é inerte e fica no caminho crítico de resposta do WhatsApp, e a
mudança foi feita horas antes de um teste ao vivo. **Fica como limpeza dedicada** — a
regra do repo é não misturar movimentação estrutural com mudança de comportamento.

## Pendências

1. **Deploy do secretarIA** — a mudança só vale depois que o serviço subir.
2. **Limpeza do caminho morto** acima, como round separado.
3. **Docs anteriores ficaram parcialmente históricos**: `CHECKPOINT_fixed_greeting_buttons.md`
   (§2 inteira descreve o short-circuit agora inalcançável),
   `CHECKPOINT_trio_gerenciar_scoped_help.md` (§1 fala em `flows_enabled=True` como um dos
   dois cohorts) e `CHECKPOINT_multi_doctor_flow.md` (cita `initial_flows` como "knob
   natural"). Não reescritos: descrevem corretamente o que era verdade no round deles.
   Este arquivo é o ponteiro.
