# CHECKPOINT — alerta de profissional com configuração incompleta (FEAT 41 + FIX 34)

**Estado:** FEAT 41 no ar desde 2026-08-29 (`c1d76c2`, deploy provado). **FIX 34 BUILT e
testado (1856 verdes + 318 no frontend), não commitado, não deployado** — remove a coluna
`professionals.email` que o FEAT 41 criou e passa a resolver o endereço do médico pelo
brain-api. Ver §4 (reescrita), §5 e a ordem obrigatória em §9.
**Origem:** incidente do tenant "Chrysostomo For Eyes" (2026-08-28) — dois profissionais
ativos com configuração incompleta; o paciente batia numa parede e ninguém ficava sabendo.
**Prompts:** `TECH/BRAIN/z_prompts/debug_secretaria_producao/PROMPT_FEAT_41_PROFESSIONAL_CONFIG_GAP_ALERT_BACKEND.md`
e `.../PROMPT_FIX_34_PROFESSIONAL_EMAIL_DUPLICATE_SOURCE.md`

---

## 1. O problema: duas falhas que pareciam a mesma

Do lado do paciente, estas duas situações eram indistinguíveis — e são opostas:

| Situação | Natureza | Quem resolve |
|---|---|---|
| A agenda do profissional está **cheia** na janela consultada | normal, se resolve sozinha | ninguém, é a vida |
| O profissional **não tem horário (ou serviço) configurado** | erro de cadastro | só um humano, no painel |

As duas caíam no mesmo `NO_AVAILABLE_DAYS_MESSAGE` ("Não encontrei horários livres nos
próximos dias"). O paciente recebia uma resposta educada, a conversa voltava pro menu, e a
clínica **não era avisada de nada**. Um médico podia ficar meses invisível pra agendamento
sem que ninguém percebesse.

## 2. A separação

O check novo é **estático** — lê configuração armazenada, nunca a agenda — e roda **antes**
de qualquer chamada ao Google Calendar. É isso que o distingue do branch `if not days:`
dentro de `enter_day_picker`, que continua existindo e continua servindo a agenda cheia.

### Onde cada metade vive

- **`services/flow_router.py` (puro — decide, não avisa ninguém)**
  - `PROFESSIONAL_GAP_HOURS` / `PROFESSIONAL_GAP_SERVICES` — os dois códigos de gap.
  - `PROFESSIONAL_NO_HOURS_MESSAGE` — copy própria, deliberadamente **diferente** de
    `NO_AVAILABLE_DAYS_MESSAGE`; manter as duas strings separadas é o que impede os dois
    casos de colapsarem de novo num só.
  - `FlowRouterResult.action` ganhou `"professional_config_incomplete"`; o campo novo
    `professional_config_gap` diz QUAL configuração falta. QUEM está sem ela viaja no
    `flow_selected_professional_id` que já existia — não há campo paralelo.
  - `_professional_config_incomplete(...)` — o construtor único dos dois gaps.
  - **Gap de serviço:** `_enter_professional_services` já tinha o check certo (lista vazia
    de `professional_appointment_types`); só passou a *sinalizar*. As bolhas enviadas ao
    paciente são idênticas às de antes.
  - **Gap de horário:** o guard mora em `_ask_day`, e ali por um motivo — é o funil único
    por onde passam TODAS as primeiras entradas no day picker de agendamento (o toque em
    "Sim, agendar", a resposta do convênio, o hand-back da LLM `enter_guided_booking`, e o
    resume de `resume_bubbles`). Um quinto chamador não consegue burlar. O
    `enter_day_picker` em si ficou de fora porque o branch de remarcação o compartilha e
    tem outro remédio.
  - `_booking_professional(...)` resolve DE QUEM é a agenda: o médico escolhido, ou — numa
    clínica de um profissional só, onde nada nunca escolhe — a única linha ativa, que é a
    configuração efetiva da clínica (é assim que `_flow_tenant_snapshot` já resolve tudo).

- **`workers/tasks.py` (efeito colateral)**
  - `_handle_professional_config_incomplete(...)`, espelhando
    `_handle_calendar_unavailable`, com três diferenças deliberadas:
    1. **Sem handover.** Um apagão de Calendar quebra a clínica inteira e uma secretária
       humana assume; um médico sem horário quebra só aquele médico, e nenhum humano no
       chat inventa uma agenda. O que falta é uma mudança de configuração — que é
       exatamente o que o e-mail pede.
    2. **Dois destinatários**, cada um só se for CONHECIDO: `tenants.contact_email` (a
       clínica) e o endereço do próprio médico — este **perguntado ao brain-api** a cada
       alerta (`services/brain_professionals.py::fetch_professional_emails`), nunca
       guardado aqui (ver §4). Nenhum dos dois é um no-op normal, nunca um erro — o
       paciente já foi respondido.
    3. **Chave Redis e janela de silêncio próprias.**

- **`services/email.py`** — `send_professional_config_incomplete_alert(...)`, fail-open e
  silenciosa sem `SMTP_HOST`, como os três alertas irmãos. O corpo diz **qual paciente**
  (nome), **qual número**, **qual profissional** e **o que falta configurar**, com o passo
  concreto pra resolver.

## 3. Debounce — por que uma chave nova

```
professional_config:alert:{tenant_id}:{professional_id}:{gap}
```
TTL: `PROFESSIONAL_CONFIG_ALERT_SILENCE_SECONDS` (novo em `config.py`, default `14400` = 4h).

Reaproveitar `calendar:alert:{tenant_id}` teria dois defeitos, e os dois são a mesma
doença: **um incidente mascarando outro**. Um apagão do Google silenciaria um gap de
configuração por quatro horas; um médico sem horário silenciaria o alerta de todos os
outros médicos. O escopo por `(tenant, profissional, gap)` é o que garante que dois
problemas distintos gerem dois avisos distintos — e que dois pacientes esbarrando no MESMO
problema gerem um só.

O debounce é resolvido **depois** dos destinatários: uma clínica sem nenhum endereço no
cadastro não queima uma janela de silêncio de 4h com um e-mail que ninguém recebeu.

## 4. O endereço do médico — o brain-api é dono (corrigido pelo FIX 34)

> **Esta seção foi reescrita.** A implementação original do FEAT 41 criou uma coluna
> `professionals.email` (migração `f3a9c1d7b2e4`). Era uma **segunda cópia** de um dado que
> o `brain-api` já possui, e que os dois repos já proibiam POR ESCRITO. O `FIX 34` desfez
> isso; o texto abaixo descreve o estado atual.

O endereço de um profissional mora em `users.email` do `brain-api` (ligado por
`users.professional_id`, escrito pelo fluxo de convite). O `brain-api` é o **único escritor
de identidade**, e diz isso na docstring de
`api/internal.py::internal_professional_emails`: *"secretarIA has no email column on
`professionals` and deliberately will not get one, because a second copy would drift the
moment a doctor changed their address."* O módulo `services/brain_professionals.py` deste
repo repete a regra desde o FEAT 33.

O handler resolve o endereço pelo cliente que já existia:

```
fetch_professional_emails(tenant_id) -> dict[str, str] | None
```

Três respostas, três comportamentos — os mesmos de
`plugins/professional_notification.py`, que usa esse cliente desde o FEAT 33:

| resposta | significado | o que o alerta faz |
|---|---|---|
| `dict` com a chave | médico tem usuário vinculado | manda pros DOIS endereços |
| `dict` **sem** a chave | médico sem usuário vinculado (criado sem convite) | só a clínica, **em silêncio** — é o estado normal, não um erro |
| `None` | brain-api não soube responder (rede, não-200, settings) | só a clínica, **com `logger.warning`** — "não sabemos" não é "não tem" |

A chamada acontece **depois** que a sessão do banco fecha: uma chamada de rede não segura
uma conexão de DB aberta. E acontece **depois** do check de tenant cruzado, então uma linha
de outro tenant nem custa um request ao brain-api.

**Por que a coluna era um defeito e não só uma redundância:** ela nascia `NULL` em toda
clínica pré-existente e não tinha backfill possível por si mesma — o alerta chegava só ao
`contact_email` da clínica e **nunca ao médico**, embora o brain-api soubesse o endereço o
tempo todo. E as duas cópias divergiriam no instante em que alguém editasse uma das duas
telas. Com o `fetch_professional_emails` o backfill é implícito e imediato: toda clínica
cujos médicos entraram por convite passa a receber o alerta sem preencher nada.

**Migração:** `b4c2e8f1a9d3` (revises `f3a9c1d7b2e4`) dá `op.drop_column`. A original foi
**preservada**, não editada — pode já estar aplicada em produção. Ordem de deploy é o risco
central: ver §9.

**Fronteira:** nenhuma rota de `api/hub/*` escreve ou devolve endereço de profissional.
A tela que quer exibir um lê o `linked_user_email` do próprio brain-api
(`GET /doctor/professionals`), que é o que o `secretarIA-frontend` já fazia antes do FEAT 41.

## 5. Frontend (`secretarIA-frontend`) — nada a fazer

O FEAT 41 acrescentou um campo "E-mail do profissional (avisos)" no card de
`/configuracao`. O `FIX 34` **removeu** esse campo e todo o seu plumbing
(`ProfessionalProfile.email`, `applyWireProfessionalProfile` /
`buildProfessionalConfigPayload`, `ProfessionalWire.email`,
`ProfessionalConfigUpdatePayload.email`): não havia o que ele pudesse gravar que não fosse
uma segunda cópia.

Não precisa de substituto. O `linked_user_email` — que o `ProfessionalsSection.tsx` já
exibia ao lado das chips de completude **antes** do FEAT 41, vindo do brain-api — mostra o
endereço real, e diz "Sem e-mail vinculado" quando não existe.

**Ordem de deploy backend↔frontend é livre**, e isso foi verificado, não presumido:
`ProfessionalConfigUpdate` não tem `model_config`, então o default permissivo do Pydantic
v2 vale; o `extra="forbid"` está só no envelope `HubConfigurationUpdate`, e `email` viajava
aninhado dentro dele. Um frontend antigo mandando a chave removida é **ignorado**, não um
422 que derrubaria o save inteiro da Configuração.

## 6. Para o FEAT 42 (banner) — o sinal JÁ existe, sem endpoint novo

**`GET /tenants/me/professionals`** (`api/hub/professionals.py::list_professionals`,
autenticado pela sessão do hub, **sem** `X-Internal-Api-Key`) devolve, por profissional:

| campo | significado |
|---|---|
| `has_hours` | tem pelo menos uma janela de atendimento resolvida |
| `has_services` | tem pelo menos um serviço ativo resolvido |
| `has_calendar` / `calendar_source` | Calendar coberto, e por quem |
| `complete` | `has_calendar and has_hours and has_services` |

Vêm de `services/tenant_config.py::professional_completeness_item`, o mesmo cálculo que a
regra de ativação usa — então o banner e o comportamento do bot não conseguem discordar.
**O `FEAT 42` deve ler esses campos e NÃO criar endpoint.** `GET /tenants/me/config` NÃO
carrega completude por profissional (é `TenantConfigRead`, escopo de clínica) e não deve
passar a carregar: a lista já responde, e duplicar o cálculo é como duas fontes divergem.

## 7. Testes

`tests/test_professional_config_gap_alert.py` (23 casos), nos dois níveis da skill
`conversation-flow-state`:

- **Router puro:** gap de horário dispara com `action`/`gap` certos e **sem tocar o
  calendário** (`day_scans == 0`); override próprio `{}` também é gap; **regressão** — agenda
  cheia continua no `NO_AVAILABLE_DAYS_MESSAGE` e chega a consultar o calendário;
  **regressão** — profissional completo continua chegando no day picker; multi-médico julga
  só o selecionado; gap de serviço mantém as bolhas de sempre.
- **Call site (sqlite in-memory + Redis fake + `_Lookup`, o dublê do brain-api):** os dois
  endereços recebem, **e o do médico veio do brain-api** — `lookup.tenants == [tenant.id]`
  é o que prende o FIX 34, porque um handler que voltasse a ler uma coluna ainda mandaria o
  e-mail certo em quase todo teste e só um `tenants` vazio o denunciaria; as 3 respostas
  possíveis do lookup, cada uma num teste (dict COM a entrada / dict SEM a chave = sem
  vínculo, silencioso e **sem warning** / `None` = brain-api fora, **com warning** e a
  clínica ainda avisada); as 6 combinações de destinatário (ambos / só um / nenhum /
  whitespace / duplicado); nenhum destinatário não levanta e o paciente é respondido do
  mesmo jeito; **um segundo paciente no mesmo
  (profissional, gap) dentro da janela NÃO reenvia**; profissional ou gap diferente
  reenvia; sem Redis alerta sempre (fail-open); profissional de outro tenant nunca é
  notificado; nenhuma linha de log carrega nome/telefone do paciente nem os endereços.

**Prova de que o teste morde:** trocando `if not should_send:` por `if False:` em
`workers/tasks.py`, `test_second_patient_within_the_window_does_not_resend` falha (2 → 4
envios). Restaurado.

## 8. Efeito colateral encontrado durante a implementação (importante)

Os snapshots de profissional que o worker monta (`SimpleNamespace` em três pontos de
`workers/tasks.py`) carregavam `appointment_types` mas **não** `business_hours`. O guard
novo lê essa coluna, então teria estourado `AttributeError` em toda reserva em produção —
a suíte pegou. Os três snapshots agora carregam `business_hours` **verbatim** (NULL
inclusive): achatar aqui apagaria a distinção NULL-versus-vazio e faria um médico que
apenas herda os horários da clínica parecer inagendável.

## 9. Pendências

O FEAT 41 foi commitado e deployado em `c1d76c2` **com** a coluna. O `FIX 34` a remove, e a
ORDEM abaixo é obrigatória — remover uma coluna de ORM é o INVERSO de acrescentar uma.

- [ ] Commit + push do `FIX 34` (secretarIA + secretarIA-frontend).
- [ ] **Deploy do código novo na API E no worker.** Os dois mapeiam `Professional`, e são
      dois serviços EasyPanel de deploy manual e independente. Confirmar os DOIS por
      `GET /build` / `source_fingerprint` — "commitou" não é prova.
- [ ] **Só então** rodar `alembic upgrade head` (migração `b4c2e8f1a9d3`, `DROP COLUMN`).
      Se rodar antes, qualquer serviço no código velho quebra em **toda** leitura de
      profissional com `column professionals.email does not exist` — o produto inteiro pra
      quem usa profissionais, não só o alerta. O SQLAlchemy monta a lista de colunas
      explicitamente; não existe `SELECT *` que salve.
- [ ] Deploy do `secretarIA-frontend` (campo removido) — ordem livre em relação à API,
      ver §5.
- [ ] `SMTP_HOST` precisa estar configurado no EasyPanel pro alerta sair — sem ele a
      função é um no-op silencioso, por design. **Continua pendente do FEAT 41.**
- [ ] Smoke: profissional ativo com config incompleta + mensagem de paciente de teste num
      tenant real; confirmar que o e-mail chega no endereço verdadeiro do médico (via
      brain-api) e no `contact_email` da clínica.
- [x] ~~Rodar a migração `f3a9c1d7b2e4`~~ — superseded: se ainda não rodou, `alembic
      upgrade head` aplica as duas em sequência (add + drop) e o resultado é o mesmo.
- [x] `FEAT 42` (banner nos frontends) — no ar desde 2026-08-29.

**Rollback:** antes do `DROP COLUMN`, reverter o código basta (a coluna continua lá,
inofensiva). Depois dele, reverter só o código não basta — voltar a ler a coluna exigiria
uma migração nova de `ADD COLUMN`, e ela voltaria vazia. Como a coluna nunca foi uma fonte
confiável, o rollback realista é pra frente: corrigir o código que lê
`fetch_professional_emails`.
