# Subagendas no convite e em “também atende” — TASK-054

## Estado

Implementação local preparada na branch `task/TASK-054-calendar`, worktree `C:/TECH/BRAIN-worktrees/TASK-054-calendar/secretarIA`, base `1ae6823`. Integração à main local autorizada pelo dono; estado consolidado em `BRAIN/tasks/TASK-054/CALENDAR_IMPLEMENTATION.md`. Sem push, deploy ou alteração de produção. Sem migração nem mudança dos schemas de comunicação entre serviços.

## Comportamento

Clínicas em `google_calendar_mode=shared_account` tentam preparar a agenda secundária na conta Google da clínica quando:

- o provisionamento interno cria ou vincula um médico, cobrindo convite novo da equipe, convite legado e `bind_self_professional` do brain-api;
- o hub cadastra um profissional ativo;
- o hub recebe ativação explícita `is_active=true`, cobrindo o retorno a “também atende” de um proprietário/gestor já vinculado.

`services/professional_calendar_setup.py::prepare_shared_professional_calendar` concentra a preparação pós-commit. Reutiliza `ensure_professional_secondary_calendar`, que usa as credenciais da clínica, cria nome/fuso e persiste `Professional.google_calendar_id`. Identificador já vinculado é preservado. Modo pessoal e profissional inativo não criam agenda automaticamente.

A rotina bloqueia/recarrega a linha do profissional antes da criação para serializar chamadas automáticas concorrentes em PostgreSQL. SQLite dos testes não comprova esse bloqueio. As rotas explícitas individual/em lote continuam existentes; elas não participam deste novo bloqueio e não devem ser executadas simultaneamente à preparação automática do mesmo profissional. Sucesso no Google seguido de falha de persistência ainda requer reconciliação; não há transação distribuída.

O cadastro é confirmado antes da operação externa. Ausência de conexão, autorização revogada/insuficiente ou falha Google preserva o cadastro e deixa a agenda sem identificador, como no cadastro pelo hub anterior. Erro é registrado por categoria; rollback é seguido de reload para permitir serialização. A clínica pode usar a criação individual/em lote já disponível para nova tentativa; reanexar/ativar explicitamente também tenta preparar uma agenda ausente.

Não foram adicionados estado persistido de preparação, tentativa automática em background ou bloqueio de marcação. O fallback já existente à agenda geral da clínica permanece quando falta identificador individual. Isso é uma limitação material em falhas do Google e exige verificar/preparar a agenda antes da marcação real.

Consultas anteriores da agenda geral não são migradas para a subagenda nova. Ao preparar uma agenda ausente para um profissional existente, verificar consultas históricas antes de retomar alterações/cancelamentos: a resolução atual passa a utilizar a subagenda vinculada. A recuperação em lote anterior também já apresentava essa limitação.

## Validação

- Regressão reproduzida antes do ajuste: médico novo e existente voltavam sem agenda no provisionamento; reativação também não criava agenda.
- 119 testes focados passaram: provisionamento, hub de profissionais e tradução de falhas Calendar; três avisos preexistentes de depreciação do HTTP 422.
- 63 testes do brain-api passaram: equipe e onboarding, incluindo vínculo próprio.
- Ruff nos cinco arquivos de código/teste alterados e `git diff --check`: passaram.
- Suíte completa da SecretarIA: 5.044 passaram, 68 ignorados, 31 avisos de depreciação; nenhuma falha, em 403,25 s. Testes ignorados não constituem prova de integração real.
- Graphify reconstruído por AST: 13.785 nós, 35.204 arestas; zero duplicatas/arestas quebradas e um self-loop incidental. Proveniência registrada, status STALE pelo código local ainda não commitado. HTML omitido automaticamente por exceder 5.000 nós.

Não houve chamada real de criação ao Google nem marcação de consulta. Depois de integração/publicação autorizadas, validar no tenant real: convite, primeiro vínculo próprio, reativação e destino da consulta; confirmar conta/permissão Google da clínica. Para paridade de serviços, publicar API e worker da SecretarIA juntos, conforme política do projeto.
