# TASK-030 — LLM real até a criação de uma consulta

Execução em 2026-10-06, aproximadamente 12:30–12:45 BRT, pelo `agent-browser` da Vercel.
Clínica QA: Chrysostomo For Eyes. Sessão de paciente de testes já autenticada.
Código publicado anteriormente comprovado: `f91b84d`, API/worker `43086a2439e5`.

## Resultado

**Uma consulta criada e mantida, conforme autorização expressa do dono:**
Cirurgia de Catarata, **16/10/2026 às 14:40, America/Sao_Paulo**, para o próprio paciente.
Um único toque final em Confirmar. Não houve cancelamento ou remarcação dessa consulta.

**Memória conversacional PASS; transferência completa para o fluxo PARTIAL.** A LLM
recordou serviço, Particular e a última preferência de data/horário, inclusive depois de
uma correção. Entretanto, ao entrar no fluxo, voltou a pedir o dia e o horário. Foi necessário
selecioná-los novamente para chegar ao cartão e confirmar.

**Achado adicional reproduzido:** o cartão de gerenciamento mostra **17:40**, enquanto
confirmação e resposta da LLM mostram **14:40**. O instante UTC do link de confirmação é
`2026-10-16T17:40:00Z`, equivalente a 14:40 em São Paulo. O cartão está exibindo a hora UTC
sem conversão para o fuso da clínica. A consulta de teste não foi movida para compensar isso.

## Casos exercitados

| Caso | Entrada/ação | Resultado observado |
|---|---|---|
| A1 — FAQ antes do interesse em marcar | Pergunta sobre duração da cirurgia; pedido explícito para não marcar | Permaneceu na LLM, sem criar consulta. Não informou duração; prometeu confirmar com a equipe. |
| A2 — dados juntos | Para mim, Particular, catarata, 16/10/2026 às 09:00; preparar sem confirmar | Avançou direto à lista de dias, sem repetir serviço/convênio. Pediu novamente o dia já fornecido. |
| A3 — pergunta no seletor de dia | Pedi para recordar os dados da mensagem anterior | Respondeu “Não entendi a data” e repetiu os dias. Essa entrada foi tratada pelo fluxo determinístico, não pela LLM. |
| B1 — informação distribuída/FAQ | Catarata, Particular e 19/10; pergunta sobre endereço; não iniciar | Informou endereço configurado e reconheceu a preferência de data/pagamento sem iniciar. |
| B2 — correção após outra mensagem | Alteração para 16/10 às 14:40; pergunta sobre estacionamento | Reconheceu a nova preferência junto do serviço/Particular. Pediu profissional e prometeu confirmar estacionamento. |
| B3 — recordação sem repetir campos | Pedi apenas um resumo da preferência mais recente | Respondeu exatamente catarata, Particular, 16/10/2026 às 14:40, substituindo a data anterior. |
| B4 — intenção de marcar usando a memória | Autorizei iniciar com os dados conversados e primeiro médico disponível, aguardando botão final | Pediu escolha nominal de médico. Depois de informar um nome do catálogo mostrado, avançou aos dias, mas não aproveitou data/hora. |
| C1 — data passada | Digitei 05/10/2026 em 06/10 | Não avançou nem criou consulta; ofereceu datas válidas com “Esse dia não tem mais horário livre”. |
| C2 — data corrigida válida | Digitei 16/10/2026 | Ofereceu oito horários livres, incluindo 14:40. Sem fallback humano/erro de agenda. |
| C3 — seletor e resumo | Selecionei 14:40 e Responder | Cartão com Cirurgia de Catarata e 16/10/2026 às 14:40; ainda sem confirmação. |
| D1 — confirmação autorizada | Um toque em ✅ Confirmar | Mensagem de agendamento confirmado com serviço, dia, hora e link público para adicionar à agenda. Sem OTP nesta sessão já autenticada. |
| D2 — pós-agendamento | A aplicação abriu o PreCheck | Cartão de termos apareceu. Não foi aceito nem preenchido questionário clínico; voltei à aba secretarIA. |
| D3 — consulta pela LLM | Pedi agendamentos realmente salvos, sem marcar/remarcar/cancelar | Informou uma consulta futura: catarata, 16/10/2026 às 14:40. |
| D4 — comprovação pelo fluxo | `/menu` → digitei “Gerenciar consulta” | O fluxo determinístico carregou o único agendamento e mostrou seu cartão de ações. Serviço/data corretos; hora exibida 17:40, achado de fuso. |
| D5 — deixar como solicitado | Toquei somente Voltar | Retornou ao menu. Consulta mantida; nenhuma ação de Remarcar/Cancelar executada. |

No seletor, tocar no horário apenas seleciona a opção; é preciso tocar em **Responder**.
O primeiro wait nesse ponto expirou sem POST/mensagem nova; depois de Responder, o resumo
chegou normalmente. Houve também tentativas de referências de snapshot inválidas na automação;
foram corrigidas com novos seletores observados. Não são falhas do produto.

## Achados e delimitação da causa

### L1 — data/hora ficam na conversa, mas não são transferidas

Evidência nos casos A2 e B3/B4: a LLM sabe repetir os campos corretamente e mesmo assim
o fluxo pede o dia novamente. Isso distingue memória da conversa de persistência estruturada
do rascunho. Não foi feita leitura remota de `conversations.flow_draft`.

O código publicado de `ai/tools.py:1127` ainda expõe
`set_booking_draft(service, professional, insurance)`, sem parâmetros de dia/hora/pra quem.
P2a construiu a infraestrutura; **P2b ainda não conecta a ferramenta nova**, e P3 continua
dono da confirmação expressa/retenção durante perguntas. O teste não prova regressão do
resolvedor P2a nem que os planos posteriores estejam habilitados.

### L2 — pergunta livre no seletor não retorna à LLM

A3 respondeu como parser de data, sem atender à pergunta de recordação. Foi necessário
usar `/menu` → Outro para entrar explicitamente na LLM. Não classificar A3 como perda de
memória da LLM: ela não respondeu naquele turno.

### L3 — cartão de gerenciamento exibe UTC como hora local

Consulta criada às 14:40; leitura determinística posterior mostra 17:40. A leitura do código
identificou o caminho: `workers/orchestrator.py` carrega `load_upcoming_appointments` para
“Gerenciar consulta”; `services/patient_context.py` retorna o `start_at` armazenado;
`services/flow_router.py::_appt_summary` (linha 3644) formata diretamente com `strftime`,
sem conversão para o fuso. `_appt_row_label` também formata diretamente e merece cobertura
na correção futura, mas o percurso com múltiplas consultas não foi exercitado.

Em contraste, `ai/tools.py::list_patient_appointments` converte para `calendar.tzinfo` antes
de formatar. O contexto de consultas injetado no prompt também pode alimentar a resposta;
sem logs do worker, não afirmar qual ferramenta a LLM chamou. A prova de persistência é
o cartão determinístico posterior, independente da resposta livre e do histórico.

Risco observado: o paciente pode entender um horário errado ao gerenciar a consulta.
Não houve mudança de código, deploy ou correção ao vivo nesta execução de testes.

### L4 — respostas de FAQ não concluídas

Duração e estacionamento receberam promessa de confirmação com a equipe, sem resposta
concreta durante os casos. `accepts_typing=false` nas leituras indica automação ativa,
mas não prova ausência de notificações internas. Não foram inspecionados logs nem a
configuração dos fatos da clínica para determinar se o dado faltava no catálogo/prompt.

## Evidências, segurança e limites

Evidências JSON por etapa:
`C:\TECH\BRAIN\tasks\TASK-030\evidence\llm-booking-2026-10-06\`.
Principais: `02-A-all-fields.json`, `08-B-recall.json`, `10-B-professional.json`,
`14-C-recap-submitted.json`, `15-D-confirmation-result.json`,
`20-D-llm-saved-appointments.json`, `21-D-persisted-action-card.json`,
`22-D-leave-unchanged.json`, `23-D-timezone-evidence.json` e `24-cleanup.json`.

- Observador transitório somente na sessão QA; coleta de paths/status e mensagens novas,
  por diferenças de IDs contra baseline. Sem headers, cookies, tokens ou histórico completo.
- Links/e-mail/telefone/documento e nomes de profissionais mascarados no material textual.
  Nenhum código de convite, OTP, credencial ou nome do paciente registrado no relatório.
- Mensagens e leituras observadas retornaram HTTP 200, sem handover humano nesse percurso.
- A confirmação prova o caminho normal de criação. O cartão posterior veio de consulta de
  agendamentos futuros por paciente/tenant; comprova registro acessível ao produto. Não houve
  consulta SQL remota nem inspeção direta do Google Calendar; o link público usa TEMPLATE
  e sozinho não comprova a existência de um evento na agenda Google.
- Portal/LLM e fluxo atual exercitados para um serviço/profissional, paciente já autenticado.
  Não valida WhatsApp, OTP novo, concorrência, pagamentos, terceiros ou demais profissionais.
- A sessão reutiliza histórico anterior. `/menu` não é um teste de identidade recém-criada;
  não atribuir à LLM a origem de campos de paciente já existentes.
- Suíte local não repetida: código/migrations/testes não mudaram nesta rodada. A suíte
  integrada anterior (3276 passed) é histórica, não prova destes casos ao vivo.
- Consulta mantida por escolha expressa do dono. Sem outras consultas criadas, sem alteração
  de consultas anteriores, sem novo commit/push/deploy/configuração ou SQL remoto.

Conclusão de escopo: a prova solicitada até uma consulta real foi executada. A experiência
completa de aproveitar todos os campos sem repetições ainda exige P2b/P3; L3 precisa de correção
de exibição de horário e L4 de investigação do contexto de FAQ.
