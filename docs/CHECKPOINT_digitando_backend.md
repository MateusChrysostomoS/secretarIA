# Digitando — backend universal (TASK-033)

## Estado

Implementado nas worktrees TASK-033 de secretarIA e brain-api. Nenhuma migração. Tasks 1–6 e documentação 7.1 concluídas. Revisão independente única: dois achados Important corrigidos em uma rodada, com testes RED→GREEN. Em 2026-10-04 o dono autorizou testes com agent-browser, merge e push após aprovação dos testes; deploy não autorizado. Evidências locais do navegador em BRAIN tasks/TASK-033/BROWSER_RESULT.md.

## Contrato e ciclo de vida

`services/typing_indicator.py` mantém uma chave Redis `brain_message:typing:<conversation_id>:<by>` por ator. Paciente vê automação/equipe; console vê automação/paciente. Automação tem prioridade e nenhum leitor vê a própria categoria.

As chaves humanas usam `SETEX` de 6 segundos; batimentos recomendados a cada 3 segundos. O paciente só é registrado quando `handover_state == HUMAN_ACTIVE`; caso contrário, não há escrita.

Automação usa um ZSET na mesma chave: cada execução possui token único e deadline individual de 90 segundos. Lua usa `TIME` do Redis para remover vencidos e adicionar/ler membros ativos atomicamente. Ao finalizar, `ZREM` remove somente o próprio token. Novas execuções não prolongam a presença de um worker morto. A chave também recebe TTL de 90 segundos como limpeza secundária. Turnos acima de 90 segundos deixam de aparecer, limite explícito do plano.

`workers/orchestrator.py::_send_bot_reply` adquire a presença dentro do `try/finally`, que também cobre cancelamento durante a gravação inicial. Exceção, conclusão ou cancelamento retiram somente o token da execução. WhatsApp não ativa indicador.

Todo acesso Redis falha aberto e tem orçamento de 100ms por operação. Pool ausente, erro, falta de método ou demora não derrubam o turno; logs novos contêm apenas categoria/operação/tipo do erro. Redis deve suportar Lua/TIME/ZSET; falta dessa capacidade omite o indicador, sem quebrar o atendimento.

`api/internal.py::list_brain_message_messages` acrescenta `typing=False`, `typing_by=None`, `accepts_typing=False`. O último fica verdadeiro somente com humano conduzindo. Paginação `has_more`/`before` preservada.

`POST /internal/brain-message/typing` exige chave interna e corpo fechado (`tenant_id`, `external_id`). Lookup considera tenant da conversa e do paciente, canal Portal e identidade externa. Desconhecido, WhatsApp ou automação conduzindo: `applied:false`, sem escrita. Com humano conduzindo: `applied:true`, inclusive se Redis falhar; a confirmação significa elegibilidade/aceite, não garantia de persistência.

Hub: `GET /tenants/me/conversations/{id}/typing` expõe `typing`/`by` (`automation|patient|null`); `POST` grava equipe apenas no Portal. Ambos reutilizam autenticação e lookup por tenant; conversa alheia recebe 404. WhatsApp não expõe indicador mesmo com chave residual. `send_message` limpa equipe após commit bem-sucedido de texto ou arquivo; falha mantém a marca até expirar.

Relay/contrato canônico: brain-api `docs/PORTAL_MESSAGING_API.md`, seção Typing. Sessão de conta ou visita é a única fonte do escopo; corpo fechado; autorização de produto/canal e fechamento DB antes da rede. PreCheck sem rota responde false localmente.

## Validação

- Baseline focado: 61 passed. Tasks 1–5 foram executadas em RED→GREEN, com vizinhos (97/82/92/66 passed em rodadas anteriores).
- Revisão: cancelamento inicial e quatro combinações de ordem de conclusão/cancelamento RED→GREEN; expiração independente de dono morto RED→GREEN. Typing final: 29 passed.
- Scripts Lua reais executados localmente via `fakeredis[lua]`, acrescentado somente ao grupo dev e lock. Sem Docker disponível e sem Redis remoto. Asserção do TTL configurado congela o relógio do emulador, pois tempo restante já pode ter caído de 6 para 5 segundos sob carga.
- Focado final ampliado: 227 passed, incluindo hub, anexos, segurança do turno, pipeline, mensagens recentes, entrega, abertura e camadas.
- Suíte completa pré-merge: 2855 passed, 10 skipped, 12 warnings em 182.58s, exit 0. As duas falhas Calendar previamente comprovadas no baseline foram resolvidas nos próprios testes: fixtures agora substituem `_appointment_calendar` pelo fake já usado no arquivo. RED: ambos falhavam sem credenciais; GREEN: 56 testes de action_buttons/typing passaram. Nenhuma credencial real ou fonte Calendar foi alterada.
- `ruff check .` passou; formato dos dois arquivos novos passou. Dívida de formato pré-existente: HEAD129 arquivos, atual126. Orchestrator não foi reformatado inteiro.
- brain-api: suíte hermética1252 passed/3 skipped; focado92 passed; integrador reexecutou12 testes novos e lint, ambos verdes.

Ledger e logs em `.superpowers/sdd/2026-10-01-digitando-backend/`, scratch ignorado e preservado como evidência. Registro completo: BRAIN `tasks/TASK-033/FINAL_RESULT.md` e `REVIEW.md`.

## Decisões e limites

PowerShell substitui helpers bash; dois asserts de envelope vazio foram adaptados aos campos aditivos. Cleanup inclui anexos. Deadline Redis curto pode omitir indicador em rede lenta. A solução por token/Lua é ajuste ao desenho simples do plano necessário para cancelamento/concorrência. Categoria staff é compartilhada por conversa; outro colega da equipe não é identificado. PreCheck e frontend possuem planos próprios.

Graphify AST update e diagnóstico executados em scratch, sem duplicatas/arestas penduradas; grafos versionados originais restaurados para preservar escopo do diff. Não se reivindica FRESH para esta árvore não commitada.

## Deploy e limites da prova local

NOT AUTHORIZED. Ordem: **secretaria_api + secretaria-worker → brain-api**. API e worker são serviços separados e ambos precisam da alteração.

Prova local com agent-browser 0.34.0 e Chrome: build real do Brain-Message-Frontend, rotas/autenticação reais dos dois backends, HTTP do mesh em loopback, bancos SQLite fictícios, Redis emulado com Lua e fila/LLM controlados. Automação nas duas telas, concorrência, cancelamento, humano nas duas direções, ausência da própria categoria, expiração humana, envio/cleanup, isolamento entre clínicas, corpo fechado e chat sem Redis passaram. Ver BRAIN tasks/TASK-033/BROWSER_RESULT.md para a expiração da automação e evidências.

Nenhuma operação remota ou produção. Redis real, PostgreSQL, latência/ACL do mesh e morte de processo do worker em ambiente implantado não foram provados. Deploy continua pendente de pedido explícito.