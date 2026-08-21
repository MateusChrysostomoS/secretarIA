# CHECKPOINT — Limites de texto do WhatsApp (constante única + corte marcado + trava no hub)

Built 2026-08-21. Resposta ao `PROMPT_04_list_row_truncation.md` ("nome do médico aparece
cortado na lista"). Toca **dois repos**: `secretarIA` (rede de segurança) e
`secretarIA-frontend` (a correção de verdade).

Suite state backend: **1744 passed** (`uv run python -m pytest -q`), incluindo 20 testes
novos. `ruff check` limpo em todos os arquivos tocados. Frontend: **225 passed**
(`npm test`, 13 novos), `tsc --noEmit` limpo, `npm run build` verde.

**Status: NÃO COMMITADO, NÃO DEPLOYADO.** Sem migração. Ver "Pendências".

## O problema (e o que o relato original errava)

O relato dizia "falta truncamento". Falta não era — truncamento existia em 12 lugares.
O que existia era **truncamento ruim, tarde e mudo**:

1. **Literal mágico duplicado.** `[:24]` aparecia solto em `whatsapp.py`, sete vezes em
   `flow_router.py` e uma em `booking_scope.py`. Só o limite de botão tinha nome
   (`MAX_BUTTON_LABEL_CHARS`) — e mesmo esse era usado **apenas** na validação do hub;
   o caminho de envio re-escrevia `[:20]` à mão, em seis matchers.
2. **Corte tardio.** O corte acontecia na hora de enviar. Quando o paciente via o nome
   torto, ele já estava salvo no banco havia semanas — e ninguém na clínica tinha sido
   avisado do limite.
3. **Corte mudo.** `str(name)[:24]` corta no meio da palavra sem deixar marca, então lê
   como erro de digitação, não como "encurtado".

`InviteTeamMemberModal.tsx` — o único lugar onde um humano digita o nome do profissional —
não tinha `maxLength` nenhum. O único teto era `max_length=255` no
`ProfessionalCreate`, dez vezes maior que o limite que realmente importa.

## A decisão

Duas camadas, e as duas são obrigatórias:

- **Entrada (o fix):** o hub recusa o nome enquanto a pessoa ainda pode mudá-lo.
- **Fio (a rede):** o backend continua garantindo que o payload seja aceito, para tudo que
  não passa por esse formulário (nomes cadastrados antes desta trava, nome de serviço,
  título vindo do Google Calendar, nome de usuário vindo do brain-api).

## §1 — Por que NÃO é corte por fronteira de palavra

O prompt pedia "corta no último espaço antes do limite". **Não foi implementado assim, de
propósito**, e esta é a decisão de engenharia central deste checkpoint.

Para as linhas `svc|`, o título truncado **é a chave de busca**: `svc|` não está em
`_PAYLOAD_ROW_PREFIXES` (`schemas/webhook.py`), então um toque chega como o título
*visível* e mais nada — é por isso que `resolve_service_name` compara contra o corte.
Catálogos de clínica são cheios de prefixo comum:

```
"Consulta de rotina adulto"    -> fronteira de palavra -> "Consulta de rotina"
"Consulta de rotina infantil"  -> fronteira de palavra -> "Consulta de rotina"
```

Duas linhas idênticas na tela, e `resolve_service_name` devolveria a primeira do catálogo
para qualquer um dos dois toques — **agendando o serviço errado, em silêncio**. O slice
bruto nunca teve esse defeito porque preserva a cauda que distingue.

A solução preserva a cauda e resolve a queixa real (o corte era *invisível*) marcando-o:

```
"Consulta de rotina adul…"  /  "Consulta de rotina infa…"
```

Mesmo orçamento de 24, ainda injetivo, e agora o paciente vê que foi encurtado.
`tests/test_whatsapp_limits.py::test_prefix_heavy_service_names_stay_distinguishable` e
`test_booking_scope.py::test_canonical_service_name_keeps_prefix_heavy_services_apart`
fixam isso.

## §2 — O que entrou onde

### Novo: `src/secretaria/core/whatsapp_limits.py`

Fonte única dos números (fica em `core/` porque não importa nada — respeita a regra de
camadas do `CLAUDE.md`) e das três funções de corte:

| Função | Marca? | Usada em | Por quê |
|---|---|---|---|
| `truncate_list_row_title` | sim, `…` | render **e** matcher de linha de lista | o título é chave; precisa ser injetivo e idempotente |
| `truncate_button_label` | não | render **e** os 6 matchers de botão | o hub já rejeita label > 20; marcar reescreveria as 6 chaves sem ganho |
| `truncate_plain` | não | description / section title / body | ninguém compara contra eles |

`MAX_BUTTON_LABEL_CHARS`, `MAX_GREETING_BUTTONS` e `MAX_GREETING_WITH_BUTTONS_CHARS`
continuam importáveis de `schemas/config.py` (re-export), então a validação do hub e seus
testes não mudaram de endereço.

### Chamadas trocadas (12 literais → 0)

- `services/whatsapp.py` — `send_buttons` e `send_list` inteiros; sumiram `[:24]`,
  `[:20]`, `[:72]`, `[:200]`, `[:1024]`, `[:10]`, `[:3]`.
- `services/flow_router.py` — 4 sites de render (profissional ×2, serviço ×2), rótulo de
  consulta, linhas de convênio; e os matchers `_match_professional`,
  `_matches_yes_no`, `_is_label`, o roteador de menu e o de convênio.
- `services/booking_scope.py` — `resolve_service_name`.
- `workers/tasks.py` — o matcher de hand-back da LLM.

**Render e match agora chamam a mesma função**, que é o que impede a deriva estrutural em
vez de só documentá-la.

### Frontend (`secretarIA-frontend`)

- **Novo `lib/whatsapp-limits.ts`** — espelho do cap (`MAX_LIST_ROW_TITLE_CHARS = 24`),
  o texto do tooltip, `professionalNameError` (bloqueia submit) e
  `isProfessionalNameAtLimit` (só avisa). Modelado em `lib/password-policy.ts`, que é o
  precedente do repo para "constante + validador puro consumido por um form".
  Lógica pura porque o vitest roda `environment: "node"` sem jsdom.
- **`InviteTeamMemberModal.tsx`** — `maxLength={24}`, `aria-invalid`, mensagem
  `role="alert"` e `tip` no `Field` (que já renderizava o `HelpTip` "?" — **nenhum
  componente novo foi criado**). Submit bloqueado só quando `> 24`.

**Só a variante `professional`.** Uma secretária não tem linha em `professionals` e nunca
aparece numa lista do WhatsApp — capar o nome dela seria restrição inventada. Verificado
no navegador: o modal de secretária sai com `maxLength: -1` e zero ícones de ajuda.

## §3 — O que NÃO mudou (de propósito)

- **`prof|<uuid>` continua carregando o id.** Dois nomes ainda podem colidir depois de
  qualquer corte; o id é o que torna o toque inequívoco.
- **`ProfessionalCreate.name` continua `max_length=255`.** O cap do cliente é UX, não a
  fronteira de segurança.
- **Nome de serviço (`ServiceCard.tsx`) não ganhou cap de input.** Fica só com a rede do
  backend, conforme o escopo decidido — é a segunda porta conhecida, listada abaixo.

## Validação

| Gate | Resultado |
|---|---|
| `uv run python -m pytest -q` | **1744 passed** |
| `ruff check` (arquivos tocados) | limpo |
| `ruff format --check` | `whatsapp.py` formatado; os outros 4 já estavam fora de forma **no HEAD** (dívida pré-existente, ver memória `secretaria-make-lint-red-at-head`) — as linhas novas conferem com o que o ruff quer |
| frontend `tsc --noEmit` | limpo |
| frontend `npm test` | **225 passed** |
| frontend `npm run build` | verde (14 rotas estáticas) |
| navegador, `next dev` | tooltip, cap em 24 ao digitar, erro + submit bloqueado em valor pré-existente longo, modal de secretária sem cap |

`npm run lint` **não é um gate neste repo** — `next lint` abre o assistente interativo de
criação de config ESLint (não existe config aqui). Ver `CLAUDE.md` e a skill `front-brain`.

## Pendências

1. **Commit + deploy dos dois serviços.** `flow_router.py`, `whatsapp.py` e
   `workers/tasks.py` foram tocados → **o `secretaria-worker` precisa ser deployado**, não
   só o `secretaria_api` (regra do `CLAUDE.md`; confira `GET /build` → `deploy_parity`).
2. **Deploy do `secretarIA-frontend`** (EasyPanel; nenhum `NEXT_PUBLIC_*` novo, então não
   há par `ARG`/`ENV` a adicionar no Dockerfile).
3. **Segunda porta em aberto:** o nome do profissional também pode nascer de
   `POST /doctor/professionals/self`, que deriva o nome do usuário do brain-api — ou seja,
   do `/cadastro` e do "Meu Perfil" no `brain-frontend`. Esses campos não têm cap. Só a
   rede do backend cobre esse caminho hoje.
4. **Nome de serviço** (`ServicesSection`/`ServiceCard`) segue sem cap de input.

Padrão generalizado na skill `third-party-text-limits`
(`TECH/.claude/skills/third-party-text-limits/SKILL.md`).
