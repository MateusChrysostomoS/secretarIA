# CHECKPOINT — "voltar" envia a mensagem para paciente recorrente (TASK-076)

Estado: **LOCAL**, commitado na branch `task/TASK-076-voltar-recorrente` (worktree
`C:\TECH\BRAIN-worktrees\TASK-076\secretarIA`), partindo de `main@33a4659`. Sem push, sem deploy.

## O que mudou (visão do paciente)

Quando o paciente digita **voltar** (a promessa da primeira mensagem: "Errou? Digite *voltar* a qualquer momento
para recomeçar"), a secretária envia **a mensagem que a clínica configurou em "Mensagem para paciente
recorrente"** e, junto dela, os botões do menu (Agendar / Outro, e "Gerenciar consulta" quando há consulta
marcada). Vale no WhatsApp e no Portal (mesmo caminho).

- Clínica **sem** a mensagem configurada: continua o cartão de menu de sempre.
- `/menu`, `menu`, `/reset`, `recomeçar`, `início`: continuam o cartão de menu de sempre (só `voltar` ganha a mensagem).
- **Só digitado:** o toque no botão "Voltar" dos cartões (Gerenciar consulta / edição) mantém o menu de sempre,
  sem a mensagem (`turn_router` exige que não haja `interactive_reply_id`).
- `{{name}}` é preenchido com o nome do paciente; se a mensagem ficar vazia depois de preenchida (só `{{name}}` e
  paciente sem nome), cai no cartão de menu de sempre.

## Onde (âncoras estáveis)

- `workers/shared/text.py::is_back_command` — só a palavra inteira `voltar` (sem espaços nas pontas, sem
  diferenciar maiúscula).
- `workers/shared/context.py::_ReplyContext.menu_back_greeting` — preenchido por `workers/turn_router.py` no
  bloco do comando de menu.
- `workers/orchestrator.py` (ramo `reply.menu_requested`) — monta `greeting_body` com
  `_render_greeting_template(returning_greeting_message, patient_name)`.
- `workers/shared/sentinels.py::_handle_show_main_menu(..., greeting_body=None)` — o corpo do `MenuBubble` vira a
  mensagem (cortada em `MAX_INTERACTIVE_BODY_CHARS`); os botões continuam `main_menu_buttons()`, então
  `flow_runner._offer_manage_on_main_menu` ainda acrescenta "Gerenciar consulta".
- Nada é apagado: o resto do comportamento do menu (void de sinal aguardando, descarte de dado do pagador, estado
  `MENU`) é o mesmo de antes.

## Validação

- `tests/test_menu_command.py`: 67 passaram (inclui os testes novos: predicado, mensagem + botões, sem mensagem,
  `/menu` sem mensagem, corte de mensagem longa, `voltar` no meio da marcação, mensagem que renderiza vazia,
  mensagem só com espaços, toque em botão "Voltar").
- Suíte completa: 7633 passaram, 2 falharam — `tests/test_service_change_agenda.py::
  test_a_paid_difference_that_also_moved_the_time_replans_and_tells_once` e
  `::test_a_clinic_initiated_paid_move_replans_but_sends_no_patient_change_notice`. **Ambas já falham em `main`
  limpo** (rodadas isoladas no checkout de `main`), ou seja, pré-existentes, não regressão desta tarefa.
- `ruff check`: sem achado novo (o `I001` em `workers/tasks.py` já existia).
- Frontend: nada mudou (o campo já existe no hub); prova visual no Portal exige deploy, não autorizado.

## Deploy / pendências

- Toca `workers/`: exige deploy do **`secretaria-worker`**, não só da API. Sem migração.
- Falta: mesclar em `main` (não feito), push e deploy (só com pedido explícito do dono), conferir no Portal da
  clínica de teste depois do deploy.
- Não coberto por teste: o canal Portal (caminho compartilhado, sem harness simples).
