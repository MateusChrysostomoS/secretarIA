# CHECKPOINT — CORS em formato JSON derrubou todo o portal do médico

**Data:** 2026-09-12 · **Status:** CORRIGIDO EM PRODUÇÃO E PROVADO AO VIVO
**Repos tocados:** `secretarIA` (parser) · EasyPanel `secretaria_api` + `secretaria-worker` (env)
**Prompt de origem:** `z_prompts/PROMPT_SECRETARIA_CONFIG_PROFISSIONAIS_NAO_CARREGA.md`

## Sintoma

Conta de clínica real (`Chrysostomo For Eyes`), `secretarIA-frontend`, rota `/configuracao`:
nenhuma informação da clínica carregava. Banner permanente *"Não foi possível carregar as
configurações da clínica e a lista de profissionais. Os campos ficam bloqueados até o
carregamento."* Consequência prática: impossível cadastrar profissional, logo a secretarIA
não conseguia marcar consulta para médico nenhum dessa clínica.

## Causa raiz

`CORS_ALLOW_ORIGINS` do serviço `secretaria_api` estava em **formato JSON**:

```
["https://secretaria-secretaria-frontend.cpux9k.easypanel.host","https://precheckv2-brain-message-frontend.cpux9k.easypanel.host"]
```

mas `src/secretaria/config.py::cors_origins` só sabia fazer `split(",")`. O split partiu o
valor ao meio e produziu duas entradas que **não são origens**:

```
'["https://secretaria-secretaria-frontend.cpux9k.easypanel.host'      <- sobrou o `["`
'https://precheckv2-brain-message-frontend.cpux9k.easypanel.host"]'   <- sobrou o `"]`
```

`.strip("'\"")` remove aspas, não colchetes — então o `[` da primeira entrada e o `]` da
última sobrevivem. O `CORSMiddleware` do Starlette compara o header `Origin` **exatamente**,
e nenhuma das duas casa com coisa alguma. Resultado: **400 `Disallowed CORS origin` para
TODA origem**, inclusive as duas que o operador acreditava ter liberado.

### Por que ninguém viu

- **O valor parece certo no painel.** É JSON válido, com as origens certas, escrito do jeito
  que o brain-api aceita.
- **O brain-api aceita mesmo.** Ele ganhou o ramo JSON em `46b4eb3` (2026-08-21); este
  serviço nunca ganhou. As duas metades da mesma malha discordavam sobre o formato de uma
  variável que operador copia de um painel pro outro — e o gatilho foi exatamente essa
  cópia, ao adicionar o frontend do Brain Message às duas allowlists.
- **O erro não tem status HTTP.** O preflight morre no browser; o `fetch` estoura
  `TypeError: Failed to fetch`, e o portal loga `{"event":"config_load_failed","status":null}`.
  `status: null` é a assinatura dessa falha — nenhum 4xx/5xx aparece na aba Network do jeito
  que se procura normalmente.
- **Nenhum teste pegaria.** Cada repo testa a si mesmo; o formato do valor só existe no
  EasyPanel.

### Por que derrubou a tela inteira, e não só os profissionais

`configuracao/page.tsx` é fail-closed por desenho (FIX 07) e o `hydrate()` inteiro fica atrás
de `hubTokenReady` (`page.tsx:515`). O hub token vem do brain-api por `/api/` (mesma origem,
via proxy do nginx) e **funcionou** — então a página seguiu em frente e disparou as quatro
chamadas. As três que vão direto ao hub morreram no preflight, e como o roster é
`Promise.all([getDoctorProfessionals, getProfessionals])` (`page.tsx:551`), o
`getDoctorProfessionals` (brain-api, que estava 200) foi arrastado junto pela rejeição do par.
Daí "nem os profissionais nem o resto": uma variável de ambiente de um serviço apagou a tela
toda do outro.

## Correção

**1. Produção (feito pelo usuário, sem rebuild).** `CORS_ALLOW_ORIGINS` em `secretaria_api` e
`secretaria-worker` reescrito no formato legado, separado por vírgula. Escolhido em vez de
rebuild porque o fingerprint em produção (`9e6a397ec756`) diverge do HEAD local
(`e9785845c311`) — um rebuild embarcaria commits ainda não auditados.

**2. Código (este commit).** `cors_origins` passa a aceitar os dois formatos, espelhando o
brain-api: valor começando com `[` é lido como JSON; qualquer outro cai no split por vírgula;
JSON malformado degrada para lista vazia (fail-closed) em vez de levantar exceção — `Settings()`
é construído uma vez por processo e não pode estourar. Sobe no próximo deploy normal; a partir
dele os dois formatos funcionam e essa armadilha não volta.

Testes: `tests/test_cors_origins_parsing.py` (11 casos), incluindo a string exata que estava
viva em produção.

## Prova ao vivo (2026-09-12, conta real)

Antes — preflight contra o hub:

```
OPTIONS /tenants/me/config   Origin: https://secretaria-secretaria-frontend...  -> 400
corpo: Disallowed CORS origin
```

Depois — mesmas chamadas, portal carregado:

| chamada | status |
|---|---|
| `POST /api/doctor/secretaria/hub-token` | 200 |
| `GET /api/doctor/professionals` | 200 |
| `GET [hub]/tenants/me/config` | 200 |
| `GET [hub]/tenants/me/professionals` | 200 |
| `GET [hub]/tenants/me/services` | 200 |

Preflight agora responde `200` com
`Access-Control-Allow-Origin: https://secretaria-secretaria-frontend.cpux9k.easypanel.host`.

Aba Profissionais (Section 05) renderizando dados reais: roster com **Dr. Chrysóstomo Faixa,
Dr. Diogo Raposo, Dr. Rafael Teixeira**, formulário de edição liberado
(`Especialidade: Oftalmologia`), cards com Agenda/Serviços/Horários. Banner de erro sumiu;
console **zero mensagens** num reload limpo.

Validação: `uv run python -m pytest` → 2103 passed. No `secretarIA-frontend` (não tocado):
`tsc --noEmit`, `npm test` (504 passed) e `npm run build` → todos exit 0.

## As 4 pistas do prompt — todas descartadas

1. **Deploy parcial do cookie httpOnly** — não. O `/api/` same-origin funcionou o tempo todo;
   o hub token foi mintado com sucesso (era o que deixava o `hydrate()` rodar).
2. **CORS do brain-api** — não. O brain-api tem a origem na allowlist, em JSON, e o parser
   dele lê JSON. Era o *outro* serviço.
3. **Migração `0012_role_taxonomy`** — não. Nenhuma chamada chegou a tocar o banco.
4. **Divergência `manage-api.ts` / `secretaria-hub.ts`** — não. Nenhum código de frontend foi
   alterado para corrigir o bug.

A causa era de infraestrutura, num serviço que nenhuma das pistas apontava.

## Lição

Quando dois serviços da mesma malha leem uma variável que **operador copia entre painéis**,
o parser dessa variável é contrato compartilhado. `46b4eb3` endureceu só um lado e a docstring
do brain-api ainda diz "Mirrors secretarIA's cors_origins hardening" — o espelho nunca voltou.
Ao endurecer parsing de config num serviço, propague para todo serviço que lê a mesma variável,
ou o formato novo vira uma armadilha silenciosa no primeiro lugar onde alguém copiar o valor.
