# TASK-030 — prova real da captura e das datas após deploy

Execução de 07/10/2026, aproximadamente 11:05–11:20 BRT, na Chrysostomo For Eyes, Portal secretarIA, sessão QA já autenticada. Pedido explícito do dono para testar após o deploy. Chrome DevTools MCP; sem nova publicação, deploy, alteração de configuração/interruptor ou SQL remoto.

## Resultado

**PASS no objetivo de chegar ao cartão aproveitando os dados informados.** A mensagem exata do dono, sem horário, pediu somente o horário. Não repetiu dia, médico, serviço, convênio ou para quem. Responder `14:40` por texto levou ao cartão de **07/10/2026 às 14:40**, com o profissional escolhido e Confirmar/Cancelar.

Pedidos completos com **amanhã às 14:40** e **quinta da semana que vem às 14:40** chegaram diretamente aos cartões de **08/10/2026** e **15/10/2026**, sem seletores/perguntas intermediárias. Correção só da data para 16/10 manteve os demais dados e atualizou o cartão para **16/10/2026 às 14:40**.

Nenhum Confirmar foi acionado. Rascunhos descartados antes da confirmação; a consulta previamente salva permaneceu em **16/10/2026 às 15:20**, verificada pelo gerenciamento antes e após recarregar a página. Não apareceu uma consulta futura adicional no gerenciamento. A sessão ficou no menu da secretarIA.

## Identidade implantada

- API/worker: fingerprint **6f4fae0d2333**, paridade `match`; `/health` respondeu `ok`.
- O conteúdo Python equivale ao commit **2455431**, que contém a captura publicada em **4f852a4** e a alteração posterior de atendimento humano TASK-038. Comparação feita por `git -c core.autocrlf=false archive` e pelo mesmo algoritmo de fingerprint do serviço. O código da captura está incluído neste deploy posterior.
- `build_sha`/`built_at` são `unknown`; atribuição por conteúdo, não por esses campos. Migração embarcada `e7d3c1a9b5f2`; não houve leitura do stamp remoto do banco.
- Versão/paridade conferidas no início e no fim, sem acessar ambiente/configuração do Easypanel.

## Casos

| Caso | Resultado observado | Evidência |
|---|---|---|
| Outro | Pergunta fixa “O que te traz à clínica?” | `01-outro.json` |
| Mensagem exata do dono: 07/10, médico, cirurgia de catarata, sem convênio, para si | Lista somente horários de 07/10; nenhuma pergunta repetida | `02-original-message.json` |
| Responder apenas 14:40 por texto | Cartão de 07/10 às 14:40 com médico correto | `03-typed-time-card.json` |
| Pedido completo para amanhã | Cartão direto de 08/10 às 14:40 | `04-tomorrow-complete.json` |
| Pedido completo para quinta da semana que vem | Cartão direto de 15/10 às 14:40 | `05-next-week-thursday.json` |
| Corrigir apenas a data para 16/10 | Cartão de 16/10 às 14:40; conservou médico/serviço/Particular/para si | `06-date-correction.json` |
| Verificação hipotética de 05/10/2026 | Disse que a data é passada e não pode agendar; reapresentou explicitamente o rascunho válido anterior de 16/10, ainda sem confirmação | `07-past-date.json` |
| Gerenciar consulta salva | Única consulta futura exibida: 16/10 às 15:20 | `08-saved-appointment.json` |
| Reload e nova leitura do gerenciamento | Histórico preservado e consulta original ainda 16/10 às 15:20 | `10-after-reload-saved.json` |

Na lista do dia 07/10, 14:40 foi observado entre os horários disponíveis antes de digitá-lo; nenhum horário foi escolhido automaticamente. Hoje na referência clínica era 07/10/2026, quarta-feira: amanhã corresponde a 08/10 e a quinta da próxima semana (segunda a domingo) a 15/10. A correção de data substituiu 15/10 por 16/10, sem converter a consulta já salva de 15:20 em outro horário.

## Limites e preservação

- Esta prova cobre captura/retomada até o cartão, não criação final, OTP novo, remarcação/cancelamento efetivos, pagamentos, terceiros, WhatsApp, concorrência ou todos os profissionais. Nada foi confirmado nesta rodada.
- Prova de consulta salva pelo próprio gerenciamento do produto; sem SQL remoto ou inspeção direta do Google Calendar. Não afirmar leitura independente de eventos Google nem garantia de todas as escritas externas.
- Não se consultou/alterou o interruptor v2 remoto. O comportamento desejado foi comprovado no tenant; não se atribuiu a variante de ferramenta utilizada sem logs. A implementação foi validada localmente também com o interruptor desligado. P4/P5 e o restante da ativação completa permanecem separados.
- Algumas respostas fizeram uma recapitulação textual antes do cartão/lista. Isso não impediu o pouso correto; não é prova de aderência ao futuro prompt enxuto P5.
- Reload continuou selecionando PreCheck apesar da URL de entrada com produto secretarIA. A aba foi conferida e corrigida antes de enviar qualquer mensagem após a recarga; sem aceite de termos ou respostas clínicas. A restauração da aba continua sendo observação separada de frontend.
- Observador transitório: apenas método/path/status, sem headers, cookies, tokens ou bodies. No intervalo coletado, **231 requests sem status de erro**, incluindo 17 POSTs de mensagens/opções. Removido antes do reload; conferido ausente ao encerrar.
- Artefatos sanitizados: profissionais e links mascarados, sem convite, OTP, identidade do paciente ou histórico de autenticação. Navegador/sessão mantidos abertos; rascunho vazio e menu da secretarIA no fim.
- Nenhuma mudança de código nesta rodada; a suíte local conjunta de 3709 testes é evidência da integração anterior, não destes casos reais. Não foi repetida.

Evidências em `C:/TECH/BRAIN/tasks/TASK-030/evidence/capture-postdeploy-2026-10-07/`. Resultado supera, para estes casos executados, a limitação de captura documentada em `TESTES_REAIS_p3_posdeploy_2026-10-06.md` e `TESTES_REAIS_llm_agendamento_2026-10-06.md`.
