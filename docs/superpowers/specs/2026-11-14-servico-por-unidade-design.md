# Serviço × médico × unidade no fluxo do paciente + /contexto (TASK-083, follow-up) — design

Estado (2026-11-14): **implementado nas branches locais `task/TASK-083-multi-unidades`** (secretarIA e
Brain-Message-Frontend) — **sem merge, sem push, sem deploy**. brain-api: nenhuma mudança. Estado, pendências e
ordem de deploy: `docs/CHECKPOINT_agenda_multi_unidades.md`, seção "Serviço × médico × unidade".

## 1. Intenção

Hoje a clínica diz em quais unidades cada médico atende (`professional_units`), mas não diz **quais
serviços cada médico faz em cada unidade**. O fluxo do paciente (WhatsApp e Portal) pergunta a unidade
depois de serviço, médico e convênio e lista **todas** as unidades do médico. Se um serviço só existe numa
das unidades do médico, o paciente ainda pode marcar na unidade errada.

Objetivo do dono: depois de escolher serviço e médico, o paciente só vê as unidades onde **aquela
combinação médico + serviço** é feita. A clínica configura isso na tela /contexto.

## 2. Objetivos, não-objetivos, anti-objetivos

**Objetivos**
- A lista de unidades do fluxo mostra só as unidades da combinação médico + serviço.
- Se sobrar **uma** unidade (e o médico atende em 2+), o paciente recebe a confirmação "Sim / Não" (texto
  em §5). Se sobrar nenhuma, o paciente é avisado e a clínica recebe um e-mail para corrigir.
- /contexto: a configuração exclusiva da combinação profissional × unidade (horário próprio e serviços
  feitos ali) fica sempre visível dentro de cada unidade marcada; o bloco todo é recolhível no cartão do
  profissional ("Unidades ▾") para o cartão não ficar enorme.
- Retirar o botão "Sincronizar agenda" do Portal.

**Não-objetivos**
- Preço e duração por unidade (continuam por profissional).
- Mexer em `brain-api`, Stripe, Meta, WhatsApp de produção, deploy, push ou merge.
- Mudar o comportamento de clínicas sem restrição configurada.

**Anti-objetivos (falha mesmo que "funcione")**
- Clínica existente mudar de comportamento sem ninguém ter marcado nada.
- O paciente marcar um serviço numa unidade onde ele não é feito por nenhum caminho (fluxo determinístico,
  Portal, ferramentas da IA).
- E-mail à clínica com dado de paciente, ou enchendo a caixa de entrada.
- Um serviço "restrito" voltar a valer em todas as unidades por acidente (ex.: desmarcar a última unidade).

## 3. Restrições

- Isolamento entre clínicas (tenant) em tudo: a tabela nova só liga profissional, serviço e unidade da
  MESMA clínica, validado na escrita.
- Serviço é identificado por `service_id` (catálogo canônico `services`); as entradas por profissional em
  `Professional.appointment_types` têm `service_id` opcional (backfill gradual). Entrada sem `service_id`
  = sem restrição (vale em todas as unidades) e não aparece para marcar unidade até ter `service_id`.
- Migração aditiva (tabela nova); deploy: migração → secretarIA (API **e** worker, são serviços separados)
  → frontend. Nada disso é feito por esta tarefa.
- Validação: `make test` / `make lint` no secretarIA (rodar com `BOT_ALLOWLIST_WA_IDS=""`,
  `uv run python -m pytest`; falhas já conhecidas separadas de regressões), `npm test` / `npm run build` /
  `npm run typecheck` no Brain-Message-Frontend (sem lint).
- Falar com o dono em linguagem leiga; nomes técnicos ficam em `docs/`.

## 4. Abordagens consideradas

- **A (escolhida pelo dono): tabela própria `professional_service_units`**, uma linha por (profissional,
  serviço, unidade). Regra: **sem nenhuma linha para (profissional, serviço) = vale em todas as unidades
  do médico**; com linhas = só nessas (sempre ∩ unidades reserváveis do médico). Salva junto do bloco de
  unidades (mesmo botão "Salvar unidades"); não toca preço/duração.
- B (descartada): guardar `unit_ids` dentro de `Professional.appointment_types`. Mais simples de ligar, mas a
  lista é regravada inteira a cada "Salvar configuração" e um campo omitido pelo cliente zera o dado (já
  aconteceu com as descrições).

## 5. Decisões do dono (como decididas)

1. Clínicas existentes: sem marcação = todas as unidades do médico (nada muda).
2. Mensagem quando o filtro deixa **uma** unidade (texto do dono, botões **✅ Sim** / **❌ Não**):
   "O serviço {serviço} feito por {médico} é feito apenas na unidade {unidade}, em {endereço}. Você
   confirma essa unidade?"
3. **Não** → volta para a escolha de médico e segue.
4. Nenhuma unidade possível para a combinação → e-mail para a clínica explicando o caso, com o link da rota
   /contexto, para corrigirem.
5. /contexto: dentro do cartão do profissional, um botão "Unidades ▾" abre os detalhes; o nome da seção
   passa de "Locais de atendimento" para **"Unidades"** em toda a tela. Dentro de cada unidade marcada, sempre
   visível (sem outro clique): o horário (geral ou próprio) e "Serviços feitos aqui".
6. Retirar o botão "Sincronizar agenda".

## 6. Desenho

### 6.1 Servidor (secretarIA)

- Modelo + migração Alembic: `professional_service_units(professional_id FK CASCADE, service_id FK CASCADE,
  unit_id FK CASCADE, created_at; PK(professional_id, service_id, unit_id))`.
- Serviço de domínio (ao lado de `professional_units.py`): ler/gravar as marcas de um profissional; validar
  tudo da mesma clínica; o profissional precisa estar vinculado e ativo na unidade; o serviço precisa estar
  no catálogo do profissional.
- API do hub: leitura e gravação das marcas (mesma permissão de `professional_units`: gestor ou o próprio
  profissional) **junto com os vínculos e horários, numa chamada atômica** (decisão 10 da §9; a gravação de
  "Salvar locais" hoje já é separada de "Salvar configuração": manter essa separação). Leitura por
  profissional: para cada unidade vinculada, os `service_id` marcados; serviço sem linhas aparece como "todas".
- Regra de gravação: nunca persistir o conjunto vazio como "nenhuma"; para cada serviço restrito, o
  conjunto salvo tem ≥ 1 unidade. Desvincular uma unidade que é a **única** de um serviço restrito é
  recusado com erro claro listando os serviços (a clínica reaponta antes). Ligar uma unidade nova não muda
  serviços restritos.
- Roster do roteador: cada profissional passa a levar, por serviço, o conjunto de unidades permitidas (ou
  "todas"). Uma única consulta no worker, como `unit_options_for` já faz.
- Fluxo (`flow_router._ask_day` e `flow_units`): a lista de unidades passa a ser
  `bookable_units(profissional) ∩ permitidas(serviço)`.
  - 0 → aviso gentil ao paciente + e-mail à clínica (§6.3) + volta para a lista de médicos.
  - 1 e o médico atende em 2+ unidades → passo de **confirmação** da unidade (texto da §5, dois botões fixos
    ✅ Sim / ❌ Não; o LLM só entra para conversa livre nesse passo, como nos outros passos fixos). Sim →
    segue para o dia daquela unidade (`flow_selected_unit_id` gravado). Não → lista de médicos.
  - 1 e o médico atende em 1 unidade só → como hoje (usa a unidade, sem pergunta). **[decisão minha, §9]**
  - 2+ → lista de hoje, só com as permitidas ("⬅️ Outra unidade" etc. continuam).
  - Todos os pontos que hoje oferecem "voltar para a unidade" (`BACK_TARGET_UNIT`, `unit_choice_needed`)
    passam a usar o conjunto filtrado do serviço em curso, não o do médico inteiro.
  - Troca de serviço ou de médico no meio da reserva limpa a unidade (já é assim) e refiltra.
- A mesma regra vale no Portal do paciente (mesmo roteador) e nas ferramentas da IA (`ai/unit_scope.py`,
  `create_event_at_unit`, `list_units`): a IA não oferece nem grava unidade onde o serviço não é feito.
  `check_unit` (`services/unit_booking.py`) passa a recusar a combinação inválida em qualquer caminho de
  escrita iniciado pelo paciente. O hub da equipe (encaixe permissivo) **só avisa**, não bloqueia.
  **[decisão minha]**
- Remarcar mantém a unidade da consulta (como hoje); se o serviço deixou de ser feito ali, a remarcação
  segue na unidade original (consulta já existente não é invalidada). **[decisão minha]**

### 6.2 Portal /contexto (Brain-Message-Frontend)

- Cartão do profissional: o bloco "Locais de atendimento" vira o botão **"Unidades ▾"** (recolhido por
  padrão; resumo como "2 unidades"). Ao abrir, mostra as unidades da clínica com a chave de ligar/desligar.
- Dentro de cada unidade **ligada**, sempre visível: o horário (geral ou "Definir horário próprio", como hoje)
  e **"Serviços feitos aqui"** com uma caixa por serviço ativo do profissional. Todas marcadas = "todas" (nada
  gravado); subconjunto grava a restrição. Não deixa desmarcar tudo (mensagem: para o médico parar de fazer o
  serviço, desligue-o em "Serviços oferecidos").
- Salvar no botão **"Salvar unidades"** (hoje "Salvar locais") com o mesmo "Descartar"; erros do servidor
  (ex.: desvincular a única unidade de um serviço) em linguagem leiga.
- Copy: "Locais de atendimento" → "Unidades" na tela (e nas dicas).
- **Remover "Sincronizar agenda"**: botão, `onSync`/`api.sync`, estado `syncing`, textos `sync*`,
  `canSyncAgendas` e testes; **manter** o selo "Agenda compartilhada" (diz quantas outras clínicas dividem a
  agenda do profissional, sem nomes). A rota `POST /tenants/me/agendas/sync` do servidor fica (suporte/ops,
  sem UI). **[decisão minha, §9]**

### 6.3 E-mail à clínica (nenhuma unidade possível)

- Quando o filtro deixa 0 unidades para (médico, serviço) no momento em que um paciente chega nesse passo.
- Destinatário: o e-mail operacional já existente da clínica (`Tenant.contact_email`, mesmo destino dos
  alertas de pagamento/agendamento). Sem e-mail configurado → só log (sem PII), nunca quebra o fluxo.
- Conteúdo: o serviço, o médico e o motivo em linguagem simples ("nenhuma unidade marcada para esta
  combinação está ativa e com horário") + link `{BRAIN_MESSAGE_PUBLIC_URL}/contexto/?prof=<id do
  profissional>` (a tela já aceita `?prof=`). **Nenhum dado de paciente**, nem canal, nem telefone.
- Frequência: no máximo **1 e-mail por dia por (clínica, médico, serviço)**, com o mesmo contador diário
  usado nos avisos de falha do sinal (`services/payments/deposit_failure_notice.py`).
- Texto ao paciente nesse caso: não foi possível seguir com esse serviço com esse médico agora, a clínica
  foi avisada, e oferece escolher outro médico.

## 7. O botão "Sincronizar agenda" — por que sai

Ele chama `POST /tenants/me/agendas/sync`: pede ao brain-api quem é a pessoa por trás de cada profissional
(mesma conta de equipe) e **liga** as agendas dos profissionais que são a mesma pessoa em clínicas
diferentes. Quem garante a agenda única **não é o botão**: é essa ligação feita **sozinha** (a) quando o
profissional é criado/vinculado na clínica (`try_link_professional`) e (b) a cada 15 minutos pelo cron do
worker (`run_agenda_reconcile`). Depois de ligadas, o ocupado de uma clínica bloqueia o horário na outra.
O botão só antecipa a ligação que o cron faria em até 15 minutos. Risco de retirar: se o worker (cron) não
estiver rodando em produção, uma ligação tardia só acontece no vínculo; por isso o deploy do worker é
obrigatório no checklist.

## 8. Testes e prova

- secretarIA (TDD, falha primeiro): filtro por combinação; 1 unidade → confirmação (Sim segue, Não volta ao
  médico, texto exato); 2+ → lista filtrada; 0 → aviso ao paciente + e-mail (1/dia, sem PII, sem e-mail
  configurado, falha de envio não bloqueia); sem marcas → comportamento idêntico ao de hoje; IA e
  `check_unit`; troca de serviço/médico; migração; hub API (permissões, tenant errado, desvincular a única
  unidade). Suíte do repo + `make lint` (separar falhas já conhecidas).
- Frontend: testes de visão (recolhível, caixas, sem desmarcar tudo, sem botão de sincronizar) + `npm test`,
  `npm run typecheck`, `npm run build`.
- Navegador real: stack local de `_local-test/` (segunda unidade criada nos dados fictícios), **agent-browser
  da Vercel** no Portal /contexto (marcar serviço por unidade, salvar, recarregar) e no chat do Portal
  (fluxo com 0, 1 e 2+ unidades, botões Sim/Não); e-mail conferido no coletor local (`/__mail`). Dados
  sintéticos apenas; nada de produção.

## 9. Decisões minhas (para o dono conferir)

1. Confirmação "Sim/Não" só quando o filtro **reduziu** de 2+ unidades do médico para 1; médico com uma
   única unidade continua sem pergunta. (Perguntar sempre que houver uma só opção é trocar uma condição.)
2. A rota de sincronizar no servidor continua existindo, só some da tela.
3. Hub da equipe só avisa; paciente/IA são bloqueados.
4. Remarcar não invalida consulta já marcada.
5. Desvincular a única unidade de um serviço restrito é recusado (nunca vira "todas" em silêncio).
6. Entrada de serviço sem `service_id` não é restrita e não aparece para marcar.
7. Continuar nas branches locais de TASK-083 (commits locais), sem push/merge/deploy.
8. "Não" e "0 unidades" voltam para a lista de médicos **que fazem aquele serviço**; se a clínica tem um só
   médico (ou só um faz o serviço), não há lista de médicos e o paciente volta para a lista de serviços.
   Vale também quando o paciente escolheu o serviço antes do médico.
9. O serviço **não** é escondido da lista por não ter unidade válida: o paciente pode escolhê-lo e então
   recebe o aviso e a clínica o e-mail (§6.3). Esconder faria o problema ficar invisível para a clínica.
10. "Salvar unidades" é **uma chamada só, atômica**: o front manda o estado desejado completo (vínculos,
    horários e marcas) e o servidor valida tudo junto numa transação (assim a clínica pode desvincular uma
    unidade e reapontar o serviço no mesmo salvamento). Isso troca a ideia de "endpoint próprio só para as
    marcas": as marcas entram no mesmo salvamento das unidades.
11. O serviço do paciente é identificado pela **mesma entrada do profissional** que o fluxo já usou
    (`service_id` dela), nunca por comparação de nome solta. Entrada sem `service_id` = sem restrição, por
    definição (e a tela não deixa marcar unidade para ela).
12. Marca numa unidade ligada mas sem horário (fechada) é guardada e mostrada esmaecida; para o fluxo ela
    não conta como unidade reservável (se todas as marcadas de um serviço estão fechadas/inativas, é o caso
    "0 unidades" e o e-mail sai). A recusa de "desvincular a última unidade" olha os vínculos, não o horário.
13. Quando a IA/escrita do paciente é recusada por combinação inválida, o erro é claro e a IA reoferece só as
    unidades válidas daquele serviço e médico.
14. Serviço novo no catálogo do médico nasce sem restrição (todas as unidades); a tela o mostra marcado em
    todas as unidades, sem alerta.

## 10. Deixado ao construtor

Nomes de arquivos/funções novos, formato exato do endpoint e do payload, estrutura do snapshot do roteador,
texto final das mensagens de erro e do e-mail (mantendo as regras acima), cobertura de testes além da lista.
