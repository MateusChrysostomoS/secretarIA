# CHECKPOINT — Listas longas: Portal com rolagem + busca, WhatsApp com páginas (TASK-082)

Estado (2026-11-14, S7): **mesclado no `main` local** (merge `task/TASK-082-options-api`); **sem push, sem deploy.**
Contrato: `C:\TECH\BRAIN\tasks\TASK-082\CONTRACT.md` (D1–D12). Revisão: `tasks/TASK-082/REVIEW.md` (READY após F4 + N1).
WhatsApp (páginas, "ver mais" digitado, limites no cadastro): `CHECKPOINT_whatsapp_text_limits.md` §6.

## O que entrou onde (âncoras estáveis)

- **Ponteiro do cartão (Portal).** Os construtores de lista anexam `OptionSource(kind, scope, items, pinned)`
  (`ai/formatter.py::SlotsBubble.source`): serviços (`flow_router._service_list_bubble` com `service_scope`), médicos,
  catálogo da clínica, convênios e as listas do "alterar consulta" (`services/appointment_edit_flow.py`).
  `workers/shared/bubbles.py::_send_bubble` decide por canal: remetente com `SUPPORTS_OPTION_SOURCE`
  (`services/channel_sender.py::BrainMessageSender`) grava a página 1 (nomes inteiros, sem corte de 24) + linhas fixas +
  ponteiro; o WhatsApp recebe as linhas de sempre e nunca vê o ponteiro. O `scope` fica só no servidor
  (`list_options.stored_source`); no fio vai `source` sem `scope` (`schemas/conversation.py::InteractiveRead`).
- **Rota de opções.** `POST /internal/brain-message/messages/{message_id}/options` (`api/internal.py::get_brain_message_options`),
  corpo JSON `{cursor, q, page_size}` — nada da busca no endereço. Reconstrói a lista a partir do ponteiro guardado,
  sempre filtrada pelo tenant; página de 10, cursor opaco (`list_options.encode_cursor`/`decode_cursor`), busca parcial
  sem acento/caixa/emoji sobre a lista **inteira** (`list_options.normalize_query`, corte em 60). `q` nunca vai a log.
- **Toque em linha carregada depois da página 1.** `workers/portal/inbound.py::_validated_brain_message_reply_id`
  re-resolve o id dentro do escopo do cartão e do tenant (`list_options.kind_for_reply_id` + `list_options.contains`);
  id forjado, de outro tipo ou de outra clínica é recusado. Caminho de toque do WhatsApp intocado.
- **Texto digitado numa lista = igual ao `main`** nos dois canais (correção F1, D8–D10); a busca só existe na barra
  de busca do Portal.

## Achado da S7 (corrigido, commit `4bedb8a`)

`flow_router._enter_professional_services` (lista de serviços logo após tocar num médico; também a volta da IA,
`select_professional_and_continue`) montava o cartão **sem** `scope` → no Portal, toda clínica com vários médicos
recebia a página do WhatsApp (8 + "Ver mais serviços", sem busca). Teste:
`tests/test_list_options_router.py::test_the_doctor_tap_turn_sends_that_doctors_list_with_its_pointer`.

## Prova

- Suíte (F1, mesma árvore exceto `4bedb8a`): 7976 passando, 2 falhas pré-existentes (`test_service_change_agenda` ×2).
  Depois de `4bedb8a`: ver `tasks/TASK-082/TASK.md` "Validation".
- Navegador, backend real local (S7): rolagem de 12 médicos / 37 serviços / 14 convênios, busca do item 35, toque em
  linha da 2ª leva → consulta gravada com o serviço e a médica certos. Evidência no Brain-Message-Frontend
  (`docs/evidence/task082-s7-*.png`, `docs/CHECKPOINT_portal_listas_paginadas.md`).
- WhatsApp (sem envio real): a mesma clínica gera 5 páginas de serviços (8 + "Ver mais serviços" + "Não sei"; última
  5 + "Não sei"), ≤10 linhas, ≤24 caracteres; médicos 8 + "Ver mais profissionais" + "Não sei".

## Pendências

1. **WhatsApp: dois nomes que só diferem depois do 23º caractere ficam iguais na lista** (corte de 24,
   `core/whatsapp_limits.py::truncate_list_row_title`) e o toque chega como o título visível → o segundo agenda o
   primeiro (provado: "Avaliação para cirurgia refrativa" → "…de catarata"). Já existia no `main` dentro das 9 primeiras
   linhas; com as páginas, fica alcançável em qualquer posição. Portal não é afetado (nome inteiro + toque por id).
   Decisão do dono (ver `TASK.md`).
2. Lacunas aceitas da revisão: troca do modo de convênio sem limite de 60; corrida de dois cadastros em 119 serviços;
   "ver mais" digitado gravado como o título da linha (N6).
3. Deploy (não autorizado): **API + worker** do secretarIA → brain-api → Brain-Message-Frontend → secretarIA-frontend.
   Sem migração.
