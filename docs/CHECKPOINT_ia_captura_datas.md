# TASK-030 — captura completa e datas em linguagem natural

## Estado

Correção aprovada pelo dono e commitada em `8a1aaf9`. Merge/push autorizados em 07/10/2026; a main `0ca17ac` (TASK-037 e registros reais) foi incorporada em `83dc2a4`. Código combinado validado; sem deploy pelo agente, configuração, SQL remoto ou alteração de consulta real. Registro dos SHAs publicados em `C:/TECH/BRAIN/tasks/TASK-030/results/booking-date-integration-codex.md`.

## Problema e comportamento novo

O caminho atual `set_booking_draft` aceitava apenas serviço, médico e convênio. Mesmo com a mensagem do paciente dizendo dia e para quem, esses dois campos não atravessavam a ferramenta. A variante com seis campos dependia do interruptor v2. O prompt também calculava hoje com `date.today()` do servidor e não ensinava a transferir os demais campos; próximo de meia-noite UTC, hoje podia ser o dia seguinte ao da clínica.

A ferramenta atual passa a aceitar os seis argumentos opcionais: serviço, médico, convênio, `for_whom`, `day`, `time`. Chamadas com só os três argumentos originais mantêm sua validação/entrada legada. Um pedido explícito de dia/atendido/horário usa o resolvedor fresco mesmo com v2 desligada. Isso é a correção delimitada aprovada, não ativação do restante de P4/P5 ou gerenciamento v2.

Exemplo do dono: serviço do catálogo, profissional Diogo, 07/10, sem convênio, para si. A IA passa dia canônico, próprio paciente e Particular, sem inventar horário; o resolvedor valida catálogo/profissional/convênio e oferece os horários daquele dia. Respondido o horário, os demais dados são reaproveitados para montar o cartão. Dados completos e válidos vão diretamente ao cartão. Até Confirmar não há evento, consulta ou reserva criados por esse pouso.

O prompt é atualizado por turno no fuso IANA da clínica, com hoje/dia da semana e uma tabela de vinte dias. Datas relativas usam esse calendário: amanhã = +1 dia; semana que vem = próxima semana de segunda a domingo. `07/10` sem ano usa o ano corrente da clínica; data passada não vira o próximo ano silenciosamente. Datas inválidas ou ambíguas exigem esclarecimento. A tabela não é disponibilidade: a agenda do profissional e as reservas continuam sendo consultadas pelo resolvedor.

Preferências de dia/horário ficam em `flow_draft` enquanto se pergunta convênio, médico ou serviço, também neste caminho atual. Os nomes de pacientes/terceiros não entram nos campos estruturados de destinatário, que continuam só próprio paciente/outra pessoa. A autorização de terceiros segue o fluxo existente.

## Revisão: escolha de médico

Revisão independente read-only encontrou um Important, nenhum Critical/Minor: Diogo ambíguo podia virar médico omitido e reutilizar Rafael previamente escolhido. A correção preserva essa distinção com o marcador booleano interno opcional `p_unresolved`, sem texto bruto de profissional/paciente. O fluxo pede uma escolha explícita e mantém serviço, convênio, atendido, dia e horário compatíveis; a escolha válida limpa o marcador. Médico que saiu do roster entre a ferramenta e a releitura também não é substituído automaticamente por um único prestador do serviço. Payloads normais antigos conservam as seis chaves originais; sem mudança de endpoint, banco ou migração.

Achado reproduzido RED em dois casos ferramenta/worker; extensão do mesmo limite a médico removido reproduzida em dois casos sole/multi e persistência do marcador. GREEN da suíte focada: 386 testes; subset final após persistência: 79 testes. Nenhuma nova pendência Minor.

## Validação

- Baseline intacto: 3668 passed, 10 skipped, 15 warnings in 420.13s.
- Captura original RED: 8 failed/3 passed; worker flag OFF RED: 3 failed. Módulo de datas ausente antes da implementação; dezenove casos de datas GREEN. Primeiro conjunto novo GREEN: 34 testes.
- Modelo real, sem executar ferramentas: nove avaliações da mensagem do dono (07/10, amanhã, quinta da semana que vem; três repetições cada), todas com serviço/médico/Particular/atendido/dia corretos e sem inventar horário. Mais três avaliações de resposta só 14:40, todas conservando dia escolhido. **12/12 PASS**, depois repetidas com o conjunto completo registrado do plugin de múltiplos médicos, inclusive leitoras/escritora/rota alternativa: **12/12 PASS** novamente. Modelo e limite configurados localmente, usando o prompt, base e handbacks correntes; dados sintéticos, nenhuma ferramenta executada ou diálogo de paciente real transmitido nessas avaliações. Não é inspeção das capacidades/configuração habilitadas no tenant real.
- Suíte completa final pós-correção da revisão: **3706 passed, 22 skipped, 15 warnings in 433.29s (0:07:13)**. Doze avaliações reais são opt-in e ficam skipped na suíte comum; foram executadas separadamente. Os dez skips preexistentes e quinze avisos do baseline permanecem separados desses doze skips adicionais.
- Ruff dos Python afetados, `git diff --check` e comparação AST dos handlers finais limpos. `_handle_confirmation`, `_manage_cancel`, `_manage_reschedule` continuam intactos; nenhuma migração nova.

Seis expectativas antigas de pouso em lista foram atualizadas para o cartão agora aprovado para pedidos completos. As provas de reservas, slots tardios, consentimento/terceiros permaneceram; onde necessário, há uma chamada sem horário para manter também a prova da lista. Não foram enfraquecidas para ignorar agenda ou consentimento.

A primeira suíte completa após essa correção expôs uma expectativa antiga de apagar o dia com flag OFF e 66 setups que tentavam substituir `prompts.date`, removido na correção do relógio. Os fixtures agora congelam o instante UTC do helper de data da clínica, não a data do servidor. Só o bloco de calendário da referência golden foi atualizado; os demais contratos de fatos/orientações foram preservados. Suíte desses casos: 82 passed com duas comparações golden ainda antigas; após renovar o bloco, ambas passaram. A suíte completa final acima inclui todos esses ajustes.

## Âncoras

- `services/booking_dates.py::clinic_today`, `resolve_booking_day`, `date_context`.
- `ai/prompts.py::secretary_system_prompt`, `_format_conversation_state`.
- `ai/tools.py::set_booking_draft`, `_draft_professional_id`, `BookingDraftRequested`.
- `services/booking_draft.py::BookingDraft`, `_check`, `_pending`, `_resolve`, `_express_confirmation`.
- `workers/shared/sentinels.py::_handle_set_booking_draft`; `draft_resolution.py::_fold_answer`.
- Regressões em `test_booking_date_language`, `test_booking_capture_legacy`, `test_booking_capture_worker`; avaliações em `tests/llm_eval/test_booking_date_capture.py`.

## Decisões e limites

A aprovação desta correção supera a preservação anterior da ferramenta/prompt legados para a captura destes campos; a futura etapa P5 deve atualizar sua comparação de bytes do v1 para este novo baseline. O restante da ativação v2 permanece separado.

O reviewer deixou fora: comportamento arbitrário do modelo em várias mensagens; produção/Calendar/WhatsApp/Portal; resultado final da suíte; graph/docs; implementações finais antigas além da fronteira de integração. Suíte, amostra real e documentos são verificados pelo executor; os outros itens continuam sem certificação nova. As corridas existentes de confirmação final, concorrência Calendar/banco, privacidade de todas as tools e demais P4/P5 não foram reescritos.

Graphify AST atualizado com a versão canônica0.9.5 pelo fallback Python previsto na política (executável bloqueado pelo Controle de Aplicativo). Scratch permanece excluído. Proveniência gerada para este código local; alterações sem commit deixam estado STALE por política. Artefatos/logs/ledger em `.superpowers/sdd/2026-10-06-booking-date-capture/`; revisão de índice gerado separada de código na futura integração.

A produção continua no deploy anterior até publicação/deploy autorizados. Para entregar esta correção: integrar e publicar a versão validada; depois API e worker juntos. Nenhuma migração nova. Retestar a mensagem original e a resposta de horário no tenant de teste depois do deploy; os doze ensaios de modelo não são uma afirmação de que o fluxo já mudou em produção. Worktree e branch preservados.


## Movimento concorrente da main

Ao encerrar, a main local estava limpa em 0ca17ac, incluindo TASK-037 (Outro com pergunta fixa) e registros de testes reais integrados por outra execucao. Este executor nao alterou esses commits. Esta correcao foi validada no worktree sobre 01f2af7; a futura integracao deve incorporar/preservar a TASK-037 e validar o resultado combinado antes de publicar.

## Integração/publicação autorizadas — 2026-10-07

Commit da correção `8a1aaf9`; merge da main `0ca17ac` em `83dc2a4`. Código sem conflito; único conflito no checkpoint P3 resolvido preservando o reteste real e a correção em ordem cronológica. Alteração de Outro (TASK-037) e todos os documentos já commitados na main preservados.

Suíte completa combinada: **3709 passed, 22 skipped, 15 warnings in 164.86s (0:02:44)**. Ruff nos Python do range integrado e diff-check limpos. Índice AST combinado em `bdc21d3`, gerado no SHA do merge de código. Os commits posteriores ao merge contêm apenas índice/docs; publicação prepara deploy de API e worker juntos pelo dono, sem migração nova desta correção. Integração só encerra após main/task remotos coincidirem com o head validado, conforme relatório central. A produção ainda requer deploy/retete; os resultados de modelo são a amostra sintética descrita acima, não esse reteste de produção.
