# CHECKPOINT — Limites de texto do WhatsApp (constante única + corte marcado + trava no hub)

Duas rodadas. Resposta ao `PROMPT_04_list_row_truncation.md` ("nome do médico aparece
cortado na lista"). Toca **dois repos**: `secretarIA` (rede de segurança) e
`secretarIA-frontend` (a correção de verdade).

- **2026-08-21 (§1-§3)** — a infraestrutura + o nome do profissional.
  COMMITADO: `secretarIA` `ef8f6dd`, `secretarIA-frontend` `00f343d`.
- **2026-08-22 (§4)** — nome de serviço, convênio, e o limite dito à própria LLM.
  **NÃO COMMITADO.**

Estado atual das suítes: backend **1753 passed**, frontend **242 passed**,
`tsc --noEmit` limpo, `npm run build` verde. Sem migração em nenhuma das rodadas.
Ver "Pendências".

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
- **Nome de serviço (`ServiceCard.tsx`) não ganhou cap de input NESTA rodada.** Ficou só
  com a rede do backend — fechado depois, na §4.

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

## §4 — Segunda rodada (2026-08-22): os outros campos, e a própria LLM

A primeira rodada capou UM campo. Esta fecha os outros dois e tira a LLM da
dependência do corte silencioso. Backend **1753 passed** (+9), frontend
**242 passed** (+12).

### O que a varredura achou: são TRÊS campos, não dois

Todo texto escrito pela clínica que vira título de linha de lista:

| Linha | Origem | Campo no hub | Estado |
|---|---|---|---|
| `prof\|` | `professional.name` | `InviteTeamMemberModal` | capado na 1ª rodada (bloqueia submit) |
| `svc\|` | nome do serviço | `ServiceCard.tsx` | **capado agora** (avisa, não bloqueia) |
| `ins\|` | plano de convênio | `ContextSection.tsx` "Convênios aceitos" | **capado agora** (avisa, não bloqueia) |

Convênio não estava previsto em lugar nenhum — tem render em
`flow_router.py::_enter_insurance_step` e matcher próprio logo acima, exatamente
a mesma classe de problema.

### Por que estes dois AVISAM em vez de BLOQUEAR

O modal de convite envia uma coisa só; recusá-lo não custa nada à clínica.
`/configuracao` salva oito seções atrás de UM "Salvar configuração". Bloquear
esse botão por causa de um nome longo — quase certamente gravado antes do cap
existir, possivelmente nunca digitado por quem está na tela — sequestraria a
saudação, os horários e a política de Pix junto. Os dois campos mostram a
mensagem e continuam salváveis; a rede do backend mantém a linha legível.

### Convênio: o cap é por ITEM, e `maxLength` não serve

Um input, N planos separados por vírgula. Um `maxLength` no campo proibiria três
planos curtos perfeitamente legais. A validação roda sobre
`toWireInsurances(csv)` — a **mesma** função que monta o PUT — então o aviso e o
payload não podem discordar sobre onde termina um plano. A mensagem nomeia o
plano culpado em vez de dizer "algum nome está longo".

### UI: nada novo foi desenhado

`ServiceCard` é uma linha compacta sem label visível (só `aria-label`), então o
"?" ficou logo depois do input em vez de num label — o `HelpTip` já era usado
exatamente assim em `AvailabilitySection.tsx` ("Horário semanal"), como filho
direto de uma flex row. A mensagem ocupa linha própria abaixo da row inteira,
porque a row já quebra em viewport estreito e um texto espremido entre o nome e
a duração seria a primeira coisa a virar sopa. Convênio já tinha `<Field tip=…>`;
só o texto do tooltip cresceu.

### A LLM agora sabe o número

`ai/prompts.py`, bloco B, dizia só "rótulo curto" — instrução que o modelo não
tem como cumprir. Agora interpola `MAX_LIST_ROW_TITLE_CHARS` (importado, não
digitado) e diz a consequência:

    <iso_datetime>|<rótulo de no máximo 24 caracteres>
    O rótulo é o que o paciente TOCA, e o WhatsApp corta qualquer rótulo acima
    de 24 caracteres — escreva só a hora ("14:00") ou hora + uma palavra
    ("14:00 Retorno"), nunca uma frase.

`test_prompts.py` renderiza o prompt com a constante monkeypatchada para 99 e
exige ver "99" — é a única forma de provar que o texto lê a constante em vez de
trazer um "24" solto.

`_parse_slot_rows` (`ai/formatter.py`) agora aplica `truncate_list_row_title` no
**parse**, não só no `send_list`. Seguro porque `slot|` está em
`_PAYLOAD_ROW_PREFIXES`: o toque chega como "<rótulo> (<iso>)" e o ISO é a
chave, então o rótulo não é chave de nada. Ganho: um rótulo estourado nunca
percorre o resto da pipeline nem entra em log com o tamanho cheio.

### `[CONFIRM]` não precisou de nada — e isso está fixado em teste

`ButtonBubble.confirm_label`/`cancel_label` são `"Confirmar"`/`"Cancelar"`,
**fixos no código**; a LLM só escreve o CORPO do card. O limite de botão (20)
nunca passa pela mão dela. `test_confirm_block_needs_no_limit_because_its_labels_are_fixed`
existe para impedir que alguém "conserte" isso adicionando um cap que o modelo
não tem como violar.

### Bônus: um QUARTO literal mágico

`ai/formatter.py` declarava o próprio `MAX_LIST_ROWS = 10`, ao lado da cópia em
`core/whatsapp_limits.py` que `flow_router` e o cliente já liam — e
`tests/test_flow_day_picker.py` importava a de `formatter`. Agora é re-export,
então o import antigo continua funcionando e existe um lugar só para mudar.
`test_row_cap_is_the_shared_constant_not_a_local_copy` fixa isso.

### Validação da 2ª rodada

| Gate | Resultado |
|---|---|
| `uv run python -m pytest -q` | **1753 passed** |
| `ruff check` (arquivos tocados) | limpo |
| `ruff format --check` | `formatter.py`/`prompts.py`/`test_formatter.py` já estavam fora de forma **no HEAD**; as linhas novas conferem com o que o ruff quer |
| frontend `tsc --noEmit` / `npm test` / `npm run build` | limpo / **242 passed** / verde |
| navegador | convênio verificado ao vivo (tooltip com o número, erro nomeando só o plano culpado, Save segue habilitado). **ServiceCard não foi verificado no navegador** — "Adicionar serviço" fica desabilitado sem backend (guarda fail-closed de hidratação), então nenhum card renderiza offline |

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
4. ~~**Nome de serviço** segue sem cap de input.~~ **Feito na 2ª rodada**, junto
   com convênio (§4).
5. **`ServiceCard` não foi visto rodando.** O caminho está coberto por teste e o
   `HelpTip` tem precedente idêntico em `AvailabilitySection`, mas ninguém olhou
   o card com o "?" na tela — precisa de um hub com backend de verdade.

Padrão generalizado na skill `third-party-text-limits`
(`TECH/.claude/skills/third-party-text-limits/SKILL.md`).
