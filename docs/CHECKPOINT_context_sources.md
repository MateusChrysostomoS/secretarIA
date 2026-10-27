# CHECKPOINT — Contexto da clínica por fontes (TASK-026)

Estado: backend implementado e testado na branch `task/TASK-026-context-sources` (`d5d1431`). **TASK-067 (2026-10-21): portado para a base de `feature/out-of-mvp` (Pix + agenda nativa) no worktree `BRAIN-worktrees/TASK-067/secretarIA`, branch `task/TASK-067-out-of-mvp`, mesclado em `feature/out-of-mvp` a pedido do dono (2026-10-21) — local, SEM push, não publicado.** Frontend: Parte B da TASK-026, já em `feature/out-of-mvp` do Brain-Message-Frontend. Depende da TASK-025 (`ClinicFacts`, painel de completude). Spec/plano: `BRAIN/tasks/TASK-026/`; porte e provas: `BRAIN/tasks/TASK-067/LEDGER.md` (sessão 01).

## O que faz

O gestor entrega **uma fonte** — endereço do site, documento (PDF/DOCX/TXT/MD) ou texto colado — e a secretarIA **propõe** o preenchimento do contexto (endereço, estacionamento, como chegar, pagamento, cancelamento, documentos, FAQ, orientações por serviço e as mensagens prontas). Cada valor vem com a **citação literal** da fonte, e com o status `new | same | different` frente ao que já está salvo. **A IA nunca grava**: não existe rota "aplicar"; o que entra no formulário só vira dado no Salvar que já existia.

## Contrato (aditivo, `api/hub/context_sources.py`)

Todas autenticadas por `get_current_tenant` e filtradas pelo `tenant.id` do token (outra clínica ⇒ `404`).

| Rota | Resposta |
|---|---|
| `POST /tenants/me/context-sources` `{kind:"url",url}` ou `{kind:"text",text}` | `202 {id,status:"pending"}` |
| `POST /tenants/me/context-sources/file` (multipart, campo `file`) | `202` igual; o texto é extraído **na requisição**, o arquivo nunca é gravado |
| `GET /tenants/me/context-sources` | `{items:[{id,kind,label,status,error_code,created_at,has_proposal}]}` (20 mais recentes) |
| `GET /tenants/me/context-sources/{id}` | o item + `proposal` (`fields[]`, `services[]`) quando existir |
| `DELETE /tenants/me/context-sources/{id}` | `204` — **exclusão lógica**: a linha fica vazia (sem proposta, texto, URL nem nome) só para a cota continuar contando; some de toda leitura |

Erros: `422` (URL/limites; `detail.code` ∈ erros de URL e de arquivo abaixo), `413` (`too_large`), `429 {"detail":{"code":"source_quota"}}`, `503 {"detail":{"code":"queue_unavailable"}}` (a fonte **não** foi aceita e nenhuma linha fica).

## Como funciona

- `services/safe_fetch.py` — **único** caminho que busca URL (anti-SSRF): valida esquema/porta/credenciais, resolve o DNS por conta própria, exige que **todos** os IPs sejam públicos, fixa o IP na conexão (anti DNS-rebinding), revalida cada redirecionamento (≤ 3), 10 s, 2 MiB depois de descomprimir. As rotas só **julgam** a URL (`validate_url`, sem rede); quem busca é o worker.
- `services/text_extract.py` — HTML/PDF/DOCX/TXT → texto. Tipo pelo **conteúdo**, conferido com a extensão; PDF ≤ 60 páginas; DOCX com teto do XML; sem OCR.
- `services/context_extraction.py` — extração estruturada + verificação de evidência (sem citação literal presente na fonte, o valor é descartado) + limites do `ClinicFacts`; mensagens prontas que a fonte não trouxe podem ser **rascunhadas** (`origin:"draft"`, sem citação; regras G16 da SPEC).
- `workers/shared/context_ingestion.py` — job arq `ingest_context_source`: buscar → extrair → propor → **apagar** `source_text`/`url` (sucesso **ou** falha). Falhas viram `error_code` estável; um erro inesperado vira `model_error` e **não** relança (um retry cobraria o modelo de novo). Cancelamento do arq (timeout de 300 s ou desligamento do worker) fecha a linha como `model_error` e apaga o texto; se o arq reentregar um job cuja 1ª tentativa morreu no meio, a linha é fechada **sem** chamar o modelo de novo.
- `models/context_source.py` + migração `a1c4e7b9d2f3` (tabela nova, `down_revision = a4c7e2f9b1d3`, intacta). Na feature, a cadeia do Pix também parte de `a4c7e2f9b1d3`; o merge vazio `f3b9d1c7a5e2` une `d6f0a8c2e4b7` (Pix + agenda nativa) e `a1c4e7b9d2f3` num único head (TASK-067).
- Autorização: as rotas aceitam qualquer papel de equipe com sessão viva da clínica (verificação **fresca** no brain-api, sem cache positivo: equipe revogada recebe 401) — o mesmo portão do Salvar desses campos, que não são só do gestor. A IA nunca grava; o Salvar continua sendo o único escritor.

## Defesas de memória e CPU (revisão de segurança, 2026-10-11)

- **HTTP:** pede só `Accept-Encoding: gzip`; qualquer outra codificação (zstd, br, deflate) ou empilhada é recusada sem ler (`content_encoding`); o corpo é lido cru e inflado em `safe_fetch` com limite de saída (o httpx decodifica zstd e codificações empilhadas **sem** limite: um corpo de 16 KB chegou a 1 GiB). O logger do httpx fica em WARNING (ele loga caminho e query a cada requisição).
- **DOCX:** o tamanho no cabeçalho do zip é palavra do invasor e `read(n)` infla até `n` antes de truncar; a leitura é em blocos de 64 KiB com teto corrente.
- **PDF:** se o conteúdo decodificado das páginas passar de 1 MiB, é recusado **antes** de interpretar (o pypdf é Python puro e superlinear: 3 MB de operadores levaram 38 s); no máximo 2 extrações simultâneas.
- **Evidência:** além da citação estar na fonte, os números, links e e-mails do **valor** precisam estar na própria citação.
- **Cota:** contagem e inserção são uma seção crítica por clínica (lock em processo + `SELECT … FOR UPDATE` na linha da clínica).
- `::/96` (IPv4 "compatível") conta como não público.

## `error_code` (o que o gestor lê está em `lib/contexto/context-sources.ts` do front)

URL: `private_address`, `no_dot`, `no_host`, `scheme`, `credentials`, `port`, `invalid_url`, `unresolvable`, `timeout`, `network`, `content_type`, `content_encoding`, `bad_body`, `bad_redirect`, `too_large`, `too_many_redirects`, `http_<N>`. Arquivo: `unsupported_type`, `empty`, `unreadable`, `encrypted`, `no_text`, `pdf_unavailable`. (TASK-067: o front ganhou frase própria para `no_host`, `bad_redirect`, `content_encoding`, `bad_body` e `pdf_unavailable`, que antes caíam na genérica.) Análise: `model_error`, `bad_output`. Cota/fila: `source_quota`, `queue_unavailable`.

## Cotas e limites

A exclusão **não devolve cota** (senão excluir e reenviar seria chamada de IA ilimitada) e uma fonte excluída ainda em análise continua ocupando a vaga "em andamento" até o job terminar. Uma linha "em andamento" com mais de 15 min deixa de contar (worker morto). O upload checa a cota **antes** de ler o arquivo, e a extração de PDF/DOCX roda fora do loop do servidor.

`CONTEXT_SOURCES_PER_DAY` = 10 (24 h) e `CONTEXT_SOURCES_IN_FLIGHT` = 2, por clínica, contados no banco; `<= 0` desliga. `OPENAI_CONTEXT_EXTRACTION_MAX_TOKENS` = 8000 (o `OPENAI_MAX_TOKENS` de 2500 inclui raciocínio e cortaria a extração). Texto colado ≤ 60.000 caracteres, upload ≤ 10 MiB, URL ≤ 2.048.

## Retenção e logs (LGPD)

O texto da fonte existe só até o job terminar; fica a proposta (com as citações). Log só com `source_id`, `kind`, `host`, códigos e tamanhos — nunca texto, citações nem URL completa.

## Garantias e testes

| Garantia | Teste |
|---|---|
| SSRF: 63 casos (IP literal, DNS misto, rebinding, redirecionamento, esquema, porta) | `tests/test_safe_fetch.py` |
| PDF/DOCX/TXT, zip que não é DOCX, XML hostil, bombas | `tests/test_text_extract.py` |
| Citação literal obrigatória, limites salváveis, rascunhos | `tests/test_context_extraction.py` |
| Texto apagado ao fim do job (sucesso e falha), sem texto/URL em log, nunca re-roda | `tests/test_context_ingestion.py` |
| Isolamento entre clínicas, cotas, fila indisponível sem linha órfã, upload recusado com código | `tests/test_context_sources_api.py` |
| Tabela, cascata, cota/token default, head único (merge `f3b9d1c7a5e2`, pai original preservado) | `tests/test_context_source_model.py` |
| Autenticação real: sem token 401, equipe revogada 401 com verificação fresca, fonte da clínica do token, outra clínica não lê/lista/exclui, papéis de equipe aceitos | `tests/test_context_sources_auth.py` |
| Job registrado no worker | `tests/test_build_identity.py` |

**Prova local TASK-067 (2026-10-21, PostgreSQL 18 descartável + UI real do Brain-Message-Frontend, modelo SIMULADO):** migração subiu, desceu só o ramo de fontes e subiu de novo; pela tela, `localhost` e `169.254.169.254` recusados pelo servidor (422), texto colado → análise → conflito com "Manter o meu" por padrão e citação → selos e rascunhos → confirmação de rascunhos → "Revisei" ×3 → Salvar; nada gravado antes do Salvar; texto bruto apagado; PDF e DOCX sintéticos lidos pelos parsers reais; falha do modelo exibida sem tocar o formulário; 1280/390, claro/escuro. **Não provado:** busca de site real pela tela, worker arq real com Redis.

**Modelo REAL (TASK-067, 2026-10-21, `gpt-5-mini` = modelo publicado; job real `ingest_context_source` em SQLite em memória, material
sintético):** (1) material completo de clínica → 10 campos da fonte + 3 rascunhos + 2 orientações de serviço casadas com o catálogo,
estacionamento já salvo marcado `different`, ~56 s; (2) PDF real → 3 campos + 3 rascunhos + 1 orientação, ~30 s; (3) material com
injeção ("ignore as instruções", estacionamento grátis, telefone inventado) → o modelo **não obedeceu**: só nome e Pix com citação,
nenhum telefone nem "sem multa"; (4) texto sem relação (receita) → nenhum fato, só rascunhos. Em todos: citação literal presente na
fonte, rascunhos sem número ausente do material, sem dose nem nome de remédio, sempre remetendo ao médico; texto bruto apagado.
Observação de qualidade: quando o material não tem uma frase de apresentação, a descrição da clínica proposta pode ser só o título
("Clínica X - guia do paciente") — passa pela revisão do gestor como qualquer proposta. Uma rodada não mede variação estatística.

**Teste manual real (A6, 2026-10-11):** dez alvos hostis recusados (`169.254.169.254`, `localhost`, `127.0.0.1`, `[::1]`, `0.0.0.0`, `10.0.0.1`, `metadata.google.internal`, credenciais, `ftp://`, porta 8080); `https://example.com/` lido; `http://python.org` e `http://www.wikipedia.org` seguiram o redirecionamento para https com TLS real.

## Limite conhecido (não esconder)

Se o próprio texto malicioso **contém** uma frase verdadeira que sustenta um valor falso ("o estacionamento é gratuito" numa página hostil), a verificação de evidência não julga intenção. Por isso toda proposta passa por revisão humana e nada é salvo sem o Salvar. Número escrito por extenso ("sete dias") não é pego pela trava de rascunhos; o prompt proíbe e a revisão do médico é a garantia final.

## Deploy e reversão

Ordem: **migração → `secretaria_api` + `secretaria-worker` juntos (há um job novo no arq) → Brain-Message-Frontend**. `uv sync --frozen` no Dockerfile exige o `uv.lock` commitado (já traz `pypdf`). Reverter só as fontes: código antigo nos dois serviços, depois `alembic downgrade d6f0a8c2e4b7` e `alembic downgrade a1c4e7b9d2f3@-1` (provado em Postgres local). **Nunca** `alembic downgrade a1c4e7b9d2f3-1` nem `-1` a partir do head: descem até o ancestral comum e derrubam também as tabelas do Pix e da agenda nativa.

## Catálogo: profissionais, serviços e convênios (TASK-067, 2026-10-23, pedido do dono)

**Revisa a G10 e o "fora de escopo" da TASK-026.** A fonte agora também propõe os profissionais da clínica, os serviços (com duração,
preço e quem realiza) e os convênios aceitos. A IA continua **sem gravar**: o job só devolve a proposta (`context_sources.proposal`,
chaves novas `professionals`, `catalog_services`, `insurance`, `notes`; sem migração). Quem aplica é a tela, pelas rotas que já
existiam (ver o checkpoint do Brain-Message-Frontend).

- Módulo novo `services/context_catalog_proposal.py`: modelos de extração, verificação e montagem. Mesma disciplina da TASK-026: cada
  item tem citação literal da fonte; número, link e e-mail do valor precisam estar na citação; os **dias** da semana de um médico
  precisam estar nomeados na citação ("segundas e quartas", "de segunda a sexta", "dias úteis") e a semana passa pela mesma regra de
  validação do hub. Um item perde só a parte sem lastro (preço, horário, e-mail), não o item inteiro.
- Convênio: casa **só** com o catálogo global por nome ou alias; "Bradesco" → "Bradesco Saúde" só quando é o único candidato (vira
  decisão registrada); o que não está no catálogo vira "Outro" com a explicação do próprio texto. Sem modo escolhido na clínica, a proposta
  traz `default_mode: "shared"` (todos os profissionais aceitam todos); modo `independent` não é tocado.
- Serviço com dois médicos: mesmo valor e duração para os dois, dito em `notes`. Duração ausente: usa a padrão da clínica, dito em `notes`.
- `notes`: lista do que a IA **decidiu** (`kind: decision`, vindo da regra ou do campo `assumptions` do modelo) e do que **não conseguiu
  preencher** (`kind: gap`). A tela mostra isso ao fim da análise.
- E-mail de médico no texto: só sugestão; nunca guardado nem convidado (o convite de brain-api envia e-mail de verdade).
- **Limite de profissionais desligado** (decisão do dono): `PROFESSIONAL_LIMIT_ENFORCED=false` (padrão) em `config.py`; o mesmo
  `require_professional_capacity` serve a tela, o convite e o provisionamento. Para religar o teto do plano e o gate do add-on
  `multi_professional`, definir `PROFESSIONAL_LIMIT_ENFORCED=true`; o comportamento anterior (403/409) volta inteiro.
- Testes: `tests/test_context_catalog_proposal.py` (25, usa o texto real colado pelo dono), mais um caso em `test_context_ingestion.py` e
  dois em `test_hub_professionals.py` (o módulo roda com o limite ligado, exceto os casos "sem limite").
- **Não provado com modelo real**: a qualidade da extração com o LLM de verdade neste material (a prova usou resposta de modelo escrita à mão).
