# TASK-030 — reteste do P3 publicado

Rodada de 06/10/2026, aproximadamente 20:53–21:03 BRT, no Portal da Chrysostomo For Eyes. Chrome DevTools MCP, sessão QA já autenticada. Pedido do dono: após o deploy, continuar os testes neste tenant. Sem deploy pelo agente, ativação de configuração, SQL remoto, commit ou push nesta rodada.

## Resultado

**PASS no fluxo por seleção e na desistência antes da confirmação.** Novo agendamento e remarcação chegaram a cartões de 19/10/2026 às 14:40 com Confirmar/Cancelar. O rascunho novo foi descartado, a remarcação recusada e o cancelamento respondido com Não. A consulta existente de Cirurgia de Catarata permaneceu em **16/10/2026 às 15:20**, inclusive na leitura pelo gerenciamento após recarregar a página. Nenhum Confirmar ou Sim de cancelamento foi acionado.

**Percurso completo em linguagem natural não aprovado.** Informar serviço, médico, Particular, atendido, dia e hora ainda repetiu a pergunta para quem e a escolha do dia. Digitar o horário disponível `14:40` voltou ao seletor de dia; selecionar esse horário e tocar Responder chegou ao cartão. O pedido natural de remarcação também voltou à escolha de dia, mesmo contendo a data e o horário desejados.

Não confundir publicação do P3 com liberação da IA v2. O interruptor `initial_flows.ai_draft_v2` não foi consultado nem alterado nesta rodada; seu default é desligado e a recomendação publicada aguarda P4/P5. O comportamento observado é compatível com a entrada legada e não certifica execução do resolvedor v2. Não atribuir as perguntas repetidas a uma regressão do P3 sem provar o interruptor e a ferramenta realmente usados.

## Identidade do deploy

- `/health` respondeu `ok`; `/build` respondeu HTTP 200 no início e no fim.
- API e worker: fingerprint **a069f15823c2**, `deploy_parity=match`, migração embarcada `e7d3c1a9b5f2`.
- `build_sha`/`built_at` são `unknown`. Atribuição por conteúdo: `git -c core.autocrlf=false archive 01f2af7 src/secretaria`, 200 fontes Python ordenados e o mesmo algoritmo SHA-256 do serviço produzem **a069f15823c2**. Isso comprova o conteúdo Python do commit publicado `01f2af7181c61013a715de23bf7b72f9bb658d5a`, não um SHA fornecido pelo painel.
- A versão de migração informada pelo build é a embarcada na imagem; não foi consultado o stamp remoto do banco.

## Casos executados

| Caso | Resultado | Evidência |
|---|---|---|
| Gerenciar consulta existente | Cartão direto de 16/10 às 15:20 | `01-manage-initial.json` |
| Entrar na IA por Outro | Pergunta aberta, automação ativa | `02-enter-llm.json` |
| Novo agendamento com todos os campos | Repetiu para quem é; não chegou diretamente ao cartão | `03-complete-booking-request.json` |
| Responder para mim | Repetiu seleção do dia | `04-attendee-answer.json` |
| Digitar 19/10/2026 | Ofereceu horários, incluindo 14:40 | `05-typed-day.json` |
| Digitar 14:40 | Voltou ao seletor de dia | `06-typed-time.json` |
| Reenviar dia e selecionar 14:40 + Responder | Cartão de 19/10 às 14:40, Confirmar/Cancelar | `07-button-booking-card.json` |
| Descartar agendamento não confirmado | Ofereceu outro horário ou menu | `08-discard-booking.json` |
| Remarcação pela IA com referência e nova data/hora | Escolha do dia repetida | `09-natural-reschedule.json` |
| Digitar dia e selecionar novo horário | Cartão Remarcar para 19/10 às 14:40 | `10-reschedule-day.json`, `11-reschedule-card.json` |
| Recusar a remarcação | Voltou ao menu | `12-reschedule-rejected.json` |
| Reler consulta original | Continuou em 16/10 às 15:20 | `13-original-after-reschedule.json` |
| Abrir cancelamento | Sim/Não, horário local 15:20 | `14-cancel-preview.json` |
| Responder Não | Informou que manteve a consulta e voltou ao menu | `15-cancel-rejected.json` |
| Reload, voltar à aba secretarIA e gerenciar | Histórico preservado; consulta original 15:20 | `16-final-after-reload.json` |

Entrada, gerenciamento e cartão de cancelamento exibem 15:20, coerente com a confirmação de remarcação já registrada no histórico anterior. O antigo deslocamento de três horas não apareceu nesta rodada. A consulta de 15:20 era o baseline desta sessão; não foi inferida identidade com a consulta de 14:40 de outro relatório nem consultada a agenda Google diretamente.

## Limitações e observações

- O parser legado aceita identidade ISO do slot, não `HH:MM` isolado. A lacuna do horário digitado já foi reproduzida/documentada na rodada anterior em `results/p2b-p3-postdeploy-qa-2026-10-06.md`; este reteste a reproduz no novo deploy. Nenhuma correção de código foi feita aqui.
- Não certificados: v2 ativada, referências desconhecidas/múltiplas consultas pelo resolvedor v2, depósitos/limite de remarcação, terceiros, WhatsApp, OTP novo, concorrência, criação final, remarcação efetiva ou cancelamento efetivo. Nenhuma escrita direta no Calendar ou leitura SQL remota.
- Reload selecionou PreCheck apesar da URL inicial com produto secretarIA. Uma mensagem de gerenciamento foi enviada nessa aba antes de perceber a troca; não houve aceite de termos ou respostas clínicas. Seleção manual de secretarIA e nova leitura concluiram a prova. A restauração da aba/produto merece teste separado; não é evidência de falha do backend de agendamento.
- O preenchimento `fill` do harness não atualizou o estado React do composer para um texto longo; antes de existir POST, a tentativa foi substituída por entrada de teclado `type_text`. Sem reenviar mensagem que já tivesse POST. Não classificar esse ajuste da automação como mensagem perdida no produto.
- Observador temporário coletou apenas método/path sanitizado/status, sem headers, cookies, tokens ou bodies. No período observado: 161 requests, nenhum status de erro, 20 POSTs de mensagens/opções. A leitura direta de `/build` pelo navegador encontrou restrição de fetch entre origens; a prova de versão usou GET público fora do browser. O chat no mesmo origin respondeu 200.
- Evidências apenas da sessão QA, profissionais e links mascarados. Nenhum código de convite, OTP, identidade do paciente ou histórico de autenticação guardado no relatório. Observer removido antes do reload; navegador/sessão mantidos, aba secretarIA no menu ao encerrar.
- A suíte local conjunta de 3668 testes é a validação da integração anterior, não prova destes casos reais. Não foi repetida porque não houve mudança de código.

Artefatos: `C:/TECH/BRAIN/tasks/TASK-030/evidence/p3-postdeploy-2026-10-06/`. A liberação completa da IA continua dependendo de P4/P5 e da prova com o interruptor v2 explicitamente ligado no tenant de teste.
