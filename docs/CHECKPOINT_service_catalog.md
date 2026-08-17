# CHECKPOINT — Catálogo canônico de serviços da clínica (FEAT 35)

Built 2026-08-17. **Entrega 1 de 3.** Dá identidade estável a um serviço, no
nível onde ele realmente pertence: a clínica. Pré-requisito do `FEAT_34` §4.2
(filtrar médicos por serviço) e correção estrutural de `FIX_02` (preço do Pix
casado por nome exato) e `FIX_08` (descrições zeradas a cada save).

Continuação de `CHECKPOINT_onboarding_multiprofessional.md` (camada por
profissional) e `CHECKPOINT_booking_ownership.md` (dono do agendamento e nome
canônico do serviço no momento da reserva — este catálogo é a fonte que aquele
round assumia existir).

Suite state: **1424 passed** (`uv run python -m pytest -q`) — baseline era
1382, então **+42 testes novos, zero regressão**. `ruff check .` continua com
os mesmos **8 achados pré-existentes** (3 × `UP042`, `E501` em
`scripts/chat_tenant.py`, `api/hub/__init__.py`, `config.py`,
`tests/test_ehr_plugin.py`, `tests/test_post_booking_plugin.py`).

**Status: NÃO COMMITADO / NÃO DEPLOYADO.** Migração aditiva pronta, backfill
NÃO executado, UI ainda não feita (é a entrega 2).

---

## 1. O problema

Um serviço era uma string livre dentro de um JSON, repetida uma vez por
profissional (`professionals.appointment_types`, com fallback para
`tenants.appointment_types`). Não havia nada que ligasse a "Limpeza" da Dra.
Ana à "Limpeza" do Dr. Bruno — nem que distinguisse "Limpeza", "limpeza
dental " e "Limpeza Dental".

Consequências, todas reais:

1. **Filtrar médicos por serviço é comparação de string.** É exatamente o que
   o `FEAT_34` §4.2 precisa quando o paciente quer trocar de médico mantendo o
   serviço.
2. **O Pix casa preço por nome exato** (`deposit_lifecycle`): qualquer
   divergência de grafia → sem preço → sem cobrança, em silêncio.
3. **Descrições eram reenviadas — e zeradas — a cada save de profissional**
   (`FIX_08`), porque cada profissional tinha a sua própria cópia do texto.
4. Relatórios por serviço somam a mesma coisa em linhas separadas.
5. O paciente vê nomes inconsistentes ao trocar de médico na mesma clínica.

---

## 2. A decisão de modelagem

**Tabela nova (`services`), não JSON estruturado.** O requisito é identidade
*estável*; uma chave sintética dentro de um blob JSON não é uma identidade —
nada impede um save de reescrever o array e perder os ids, que é exatamente a
classe de bug do `FIX_08`. Uma PK real, com
`UNIQUE (tenant_id, normalized_name)`, torna o duplicado **impossível de
escrever**, não apenas desencorajado.

| Nível | Campos |
|---|---|
| Clínica (`services`) | `id`, `name`, `normalized_name`, `description`, `long_description`, `requirements`, `is_active`, `sort_order` |
| Profissional (entrada JSON, inalterada) | `service_id` (novo, opcional), `price`, `duration_min`, `is_active`, `sort_order` |

Duas decisões que o prompt deixou em aberto:

- **`requirements` ficou no nível da clínica.** A preparação para um exame é
  propriedade do *serviço*, não de quem o executa. (O prompt não listava o
  campo; fica registrado aqui como decisão.)
- **A ligação profissional→serviço é uma chave dentro do JSON existente**
  (`service_id`), não uma tabela `professional_services`. Isso é o mais aditivo
  possível: nenhuma coluna existente muda, nenhum leitor atual precisa saber
  que o catálogo existe, e não há dual-write durante o rollout. Uma tabela de
  ligação, se um dia fizer sentido, é assunto da entrega 3.

**A regra de resolução, em uma linha: id primeiro, nome normalizado depois,
string crua nunca.** Uma entrada com `service_id` resolve por id; uma sem
(todas, até o backfill) casa por nome normalizado; uma que não casa com nada
passa intacta. É isso que permite a tabela ir para produção **antes** do
backfill.

---

## 3. O que entrou (entrega 1)

### `models/service.py` + `migrations/versions/d1c2b3a4e5f6_service_catalog.py`
Tabela `services`. Estritamente aditiva: cria uma tabela, não altera nenhuma
outra, não escreve nenhuma linha, não reescreve nenhum JSON. `downgrade()`
derruba a tabela e não perde nada que existisse antes do `upgrade()` — as
listas por profissional continuam sendo a fonte da verdade nesta fase.

### `services/service_catalog.py`
O módulo de identidade e resolução (funções puras; só `load_service_catalog`
toca sessão):

| Função | Papel |
|---|---|
| `normalize` | A chave de identidade: trim, colapso de espaços, remoção de acentos, casefold. |
| `resolve_entries` | Sobrepõe o catálogo às entradas JSON: **nome e cópia** vêm da clínica, **preço/duração/oferece** ficam do profissional. Devolve exatamente o shape de dict que todo consumidor já lê. |
| `professionals_offering` | "Quem mais faz esse serviço?" — por id, não por string. Devolve `[]` quando ninguém faz, que é uma resposta legítima. |
| `find_near_duplicates` | **Só avisa.** Nada neste código funde por esse sinal. |
| `load_service_catalog` | A única leitura de sessão. |

### Resolução em runtime (nenhuma quebra)
`active_appointment_types(tenant)` e
`professional_appointment_types(professional, tenant)` ganharam um argumento
opcional `services=None`. **Omitir reproduz o comportamento de hoje** — é isso
que garante que os 1382 testes existentes continuam verdes e que um tenant sem
backfill se comporta como antes. Quem tem sessão passa o catálogo:

- `load_tenant_config` (prompt da LLM + config de runtime);
- `resolve_professional_calendar` (duração padrão do slot);
- `workers/tasks.py` — **uma** leitura por turno, e as entradas de cada
  profissional já chegam resolvidas ao `flow_router`, que continua sendo uma
  função pura sem I/O;
- `deposit_lifecycle._resolve_service_and_price` (o preço do Pix).

### `api/hub/services.py` + `schemas/service.py`
`GET`/`POST`/`PATCH` em `/tenants/me/services`. Nunca gated por entitlement —
catálogo é wiring de plataforma, não addon.

### `scripts/backfill_service_catalog.py` (para rodar na entrega 2)
Modo relatório por padrão (**não abre transação de escrita**). Agrupa por nome
normalizado, escolhe a grafia mais frequente como canônica, preserva a primeira
descrição não vazia do grupo, e **se recusa a consolidar** um tenant com grupos
de mais de uma grafia até alguém passar `--accept-variants`. Look-alikes
("Limpeza" vs "Limpeza Dental") são **listados e nunca fundidos**. Idempotente.
Nunca toca em `appointments`.

---

## 4. Contrato para a UI (entrega 2 — frontends)

A tela de configuração dos **dois** repos (`brain-frontend` e
`secretarIA-frontend`, arquivos duplicados) precisa de:

```
GET    /tenants/me/services                -> ServiceRead[]
POST   /tenants/me/services[?force=true]   -> 201 ServiceRead
PATCH  /tenants/me/services/{id}           -> 200 ServiceRead
```

`ServiceRead`: `{id, name, description, long_description, requirements[],
is_active, sort_order, created_at}`. `normalized_name` **não** é exposto de
propósito — é chave interna derivada de `name`.

Erros estruturados (mesmo shape do resto do hub, `detail={code, message, ...}`):

| Situação | Status | `code` | Extra |
|---|---|---|---|
| Mesma grafia normalizada | 409 | `service_already_exists` | `service` (o existente) |
| Nome parecido | 409 | `similar_service_exists` | `similar` (nomes) |
| Serviço de outra clínica / id inválido | 404 | `service_not_found` | — |

`similar_service_exists` é literalmente o aviso do §2.3.3: mostre a sugestão e
ofereça "usar o existente" ou "criar mesmo assim" (`?force=true`). `force`
**nunca** cria um duplicado exato — só o parecido.

Do lado do profissional, `appointment_types` passa a ser **seleção**, não texto
livre: cada entrada manda `service_id` + `price` + `duration_min` +
`is_active`. O `name` pode continuar sendo enviado nesta fase (é ignorado na
resolução quando há `service_id`); ele só sai do payload na entrega 3.

---

## 5. Testes

`tests/test_service_catalog.py` (42 novos): identidade e equivalência de
grafias; look-alikes só avisam; resolução (nome/cópia da clínica,
preço/duração do profissional, serviço aposentado some de todo mundo, entrada
desconhecida passa intacta, catálogo vazio = comportamento de hoje); dois
profissionais com o mesmo serviço resolvem para o **mesmo id**; filtro por
serviço incluindo o caso "ninguém mais oferece"; o `UNIQUE` do banco barrando o
duplicado e permitindo o mesmo serviço em duas clínicas; backfill (agrupa
equivalentes, não funde parecidos, relatório de variantes, preserva descrição,
idempotente, **não toca em `appointments`**); Pix casando preço com grafia
histórica divergente; e a API do hub inteira, incluindo isolamento entre
clínicas.

---

## 6. Pendências

1. **Entrega 2 — UI nos dois frontends** (`brain-frontend`,
   `secretarIA-frontend`): editor do catálogo em um lugar só, seleção em vez de
   texto livre no profissional, aviso de quase-duplicado (o 409 acima já
   entrega o sinal). Não feita nesta sessão porque os dois worktrees estão com
   trabalho em andamento (password reset + secretary role) — decisão explícita
   do usuário.
2. **Entrega 2 — rodar o backfill**: primeiro `--report` em produção, revisar
   os grupos `[VARIANTS]` e `[LOOK-ALIKE]` com o dono da clínica, só então
   `--apply` por tenant.
3. **Entrega 3** (só depois de a UI escrever `service_id` e o backfill estar
   completo): remover `name`/`description`/`long_description`/`requirements`
   duplicados das entradas JSON.
4. **Deploy**: API **e** worker no mesmo SHA (regra do `CLAUDE.md` — isto toca
   `workers/`). A migração é `alembic upgrade head`, passo manual.
5. **Rollback**: `alembic downgrade -1` derruba a tabela. Sem perda enquanto o
   backfill não tiver rodado; depois dele, o `downgrade` perde o catálogo mas
   **não** as listas por profissional, que continuam intactas.
