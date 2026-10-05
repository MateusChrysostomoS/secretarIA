# CHECKPOINT — Portal: a primeira mensagem chega ao ENTRAR, escolhida pelo contexto (TASK-035)

Data: 2026-10-05. Pedido do dono: "ao usuário entrar no Portal, seja entregue a(s) primeira(s)
mensagem(s) de acordo com o contexto do usuário — não quando ele apertar o botão de digitar".
Estado: **BUILT e commitado só nas branches `task/TASK-035-primeira-mensagem` (secretarIA,
brain-api, Brain-Message-Frontend). NÃO mesclado em main, NÃO pushado, NÃO deployado.**
Registro da tarefa: `BRAIN/tasks/TASK-035/TASK.md`.

## Por que o paciente só via algo depois de escrever

Só dois gatilhos pediam para a secretarIA falar sem mensagem do paciente: visita **nova**
(`POST /pending`) e link aberto com conta logada. Recarregar a página, escolher a clínica na lista
ou voltar logado não chamavam nada; e `/internal/brain-message/open` se recusava (`exists`) a
falar numa conversa com histórico.

## O que entrou

| Peça | Âncora | Papel |
|---|---|---|
| Gatilho (front) | `PortalConversation.tsx` (efeito de entrada) → `lib/real/patient-access.ts::enterThread` | uma chamada por conversa exibida; falha silenciosa |
| Gatilho (brain-api) | `api/portal/patient_access.py::enter_thread` — `POST /patient-access/threads/{product}/enter` | escopo pelo token, sem corpo; repassa a `/open` em segundo plano; PreCheck: nada |
| Rota (secretarIA) | `api/internal.py::brain_message_open` | conversa vazia → saudação (como antes); com histórico → continua `200 exists` **e** enfileira `process_brain_message_enter` |
| Regra de entrada | `workers/portal/open.py::_brain_message_entry_decision` | decide se fala (abaixo) |
| Job | `workers/portal/open.py::process_brain_message_enter` | direito de uso → marca no ledger → relê logo antes de escrever → envia; devolve a marca se nada saiu |
| Mensagem | `workers/shared/opening.py::resolve_opening_message` / `_send_context_opening` | qual das 3 mensagens |
| Pós-consulta | `services/patient_context.py::find_post_consult_followup` | derivado, sem coluna nova |
| Menu | `services/flow_router.py::main_menu_buttons` | o menu desenhado passa a ser [🗓️ Agendar, Outro] |

Os caminhos que já conheciam o paciente trocaram o menu fixo pela mensagem de contexto:
`workers/portal/identity.py::_handle_pre_consent_identity` (conta verificada) e
`workers/portal/merge.py::_finish_merge` (visita fundida na conta, depois do código).

## Qual mensagem (primeira regra que casar)

1. **Consulta viva no futuro** (`scheduled`/`confirmed`/`rescheduled`) → o cartão existente
   "Vi aqui que você já tem uma consulta marcada para …" com [Remarcar, ❌ Cancelar, Outro].
   Só a **detecção** é desta tarefa (`OpeningKind.UPCOMING`); o texto de lembrete com
   Confirmar/Remarcar/Outro é da TASK-032 (R3) e deve trocar só este ramo.
2. **Primeira aparição depois de uma consulta** → "Olá, {nome}! 😊 Como foi a sua consulta do dia
   DD/MM/AAAA com {médico}?" + o texto pós-consulta da clínica (`Tenant.post_consult_message`,
   antes sem uso em runtime) ou um texto padrão; botões [🗓️ Agendar, Outro]. Regra: a consulta
   mais recente que já terminou nos últimos `POST_CONSULT_FOLLOWUP_DAYS` (30), exceto cancelada e
   faltou, **e nenhuma mensagem na conversa desde o fim dela** — como a própria pergunta é uma
   mensagem, ela sai uma vez só. Consulta de outra pessoa: "Como foi a consulta de {atendido}…".
3. **Todo o resto** → a pergunta de menu da clínica (`initial_flows.menu_label`) ou "Como posso te
   ajudar?" com [🗓️ Agendar, Outro]. Agendar → "Essa consulta é pra você?" → convênio → …

Todas deixam a conversa em `MENU`: botões roteiam pelo `route()`, e texto livre (ex.: a resposta
a "como foi?") vai para a IA.

## Quando a entrada fica em silêncio (`_brain_message_entry_decision`)

- conversa vazia (é a porta da saudação), paciente/conversa desconhecidos;
- atendimento humano (`HUMAN_ACTIVE`);
- consentimento ainda não dado, ou um passo de identidade aberto (`AWAITING_NAME`/`_EMAIL`/
  `_EMAIL_CODE`) — achado da revisão: nunca apagar esses passos;
- última mensagem com menos de `PORTAL_ENTRY_QUIET_MINUTES` (30): recarregar no meio da conversa,
  ou a abertura que o login acabou de mandar;
- fluxo no meio (fora de IDLE/MENU) e mais novo que o intervalo de reativação da clínica (6 h).
  Depois disso a abertura substitui a oferta "quer continuar?" e o rascunho é descartado
  (coerente com a spec TASK-032 §4.3).

**Sem duplicar:** marca `brain_message_enter:{conversa}:{última mensagem}` compartilhada com a
fusão da visita, mais a janela de 30 min também na fusão — quem rodar primeiro fala. Releitura da
última mensagem logo antes de escrever: um toque que chegou no meio vence.

## Mudança que afeta o WhatsApp (decisão do dono pendente)

`main_menu_buttons` vale para todo menu desenhado (`/menu`, "Não" da reativação, ferramenta da
IA, menus de retorno do fluxo), **nos dois canais**. Antes: "Serviços e Custo/Remarcar/Cancelar/
Outro" ou o trio "Escolher médico/Escolher serviço/Outro". Os rótulos antigos continuam
funcionando quando digitados. Consequência: um paciente de WhatsApp com consulta marcada que
digitar `/menu` não vê mais um botão de remarcar (continua por texto ou por Outro).

## Ordem de deploy (nada disto foi executado)

Sem migração. secretarIA **API e worker juntos** (a rota enfileira um job que só o worker novo
conhece) → brain-api → front. Qualquer ordem é segura (cada lado novo cai em "não faz nada" contra
o lado velho), mas o recurso só funciona com os três.

## Validação

- secretarIA: suíte completa verde (números finais em `BRAIN/tasks/TASK-035/TASK.md`; base main
  `8b6eef3`: 2875 passed / 10 skipped). `ruff check` limpo; `open.py` já estava fora do
  `ruff format` na base (não reformatado).
- brain-api: 7 testes novos (`tests/test_portal_enter_thread.py`) + suíte completa.
- Front: 1508 testes, `tsc --noEmit`, `npm run build`.
- Navegador (agent-browser, apps reais das worktrees + jobs reais do worker, SQLite fictício,
  sem rede externa — `BRAIN/tasks/TASK-035/browser_harness.py` + `run_browser_proof.sh`):
  16/16 PASS — visitante novo recebe saudação + e-mail sem tocar em nada; recarregar não
  duplica; conta entra por e-mail+código e recebe o cartão da consulta marcada uma vez, com o
  histórico; recarregar logado não repete; voltando 2 dias depois de uma consulta recebe "Como foi
  a sua consulta do dia…" + texto da clínica; sem consulta recebe "Como posso te ajudar?"
  [Agendar, Outro]; tocar em Agendar abre "Essa consulta é pra você?".

## O que NÃO foi provado

Postgres real (só SQLite), Redis/arq reais, produção, WhatsApp (só o menu muda lá, coberto por
testes de unidade), e o lembrete de consulta da TASK-032.
