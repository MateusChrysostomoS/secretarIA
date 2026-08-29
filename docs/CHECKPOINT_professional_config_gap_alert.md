# CHECKPOINT — alerta de profissional com configuração incompleta (FEAT 41)

**Estado:** BUILT, testado (1857 testes verdes), **não commitado, não deployado**.
**Origem:** incidente do tenant "Chrysostomo For Eyes" (2026-08-28) — dois profissionais
ativos com configuração incompleta; o paciente batia numa parede e ninguém ficava sabendo.
**Prompt:** `TECH/BRAIN/z_prompts/debug_secretaria_producao/PROMPT_FEAT_41_PROFESSIONAL_CONFIG_GAP_ALERT_BACKEND.md`

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
    2. **Dois destinatários**, cada um só se existir: `tenants.contact_email` (a clínica) e
       `professionals.email` (o médico). Nenhum dos dois preenchido é um no-op normal, nunca
       um erro — o paciente já foi respondido.
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

## 4. `professionals.email` — coluna nova

Migração `f3a9c1d7b2e4` (revises `a7b8c9d0e1f2`): `VARCHAR(254) NULL`, sem `server_default`
e **sem backfill** — a clínica é a única parte que conhece o endereço de um médico, então
toda linha existente nasce `NULL` e todo consumidor trata `NULL` como "não há pra quem
mandar aqui", caindo no endereço da clínica.

Editável por **dois** caminhos, de propósito:
- `PATCH /tenants/me/professionals/{id}` — a edição de roster.
- `PUT /tenants/me/professionals/{id}/config` (e o agregado `PUT /tenants/me/configuration`)
  — que é o corpo que a tela **Configuração** já manda, então o endereço grava na MESMA
  transação da especialidade que fica ao lado dele na tela, em vez de exigir um segundo
  request que poderia falhar sozinho.

**Fronteira de segredo:** `email` aparece em `ProfessionalRead` e `ProfessionalListItem`, e
esses dois modelos são consumidos **exclusivamente** por `api/hub/*` — a sessão autenticada
da própria clínica. Nenhuma superfície `/internal` ou pública carrega o campo;
`GET /internal/tenants/{id}/config-status` continua reportando só
`has_hours`/`has_services`/`complete`, sem endereço nenhum.

## 5. Frontend (`secretarIA-frontend`) — escopo mínimo

Um campo "E-mail do profissional (avisos)" no card do profissional em `/configuracao`,
na segunda coluna do grid que já existia (a de "Especialidade"), pelo mesmo caminho de
save. Plumbing: `ProfessionalProfile.email` (`lib/types.ts`),
`applyWireProfessionalProfile`/`buildProfessionalConfigPayload` (`lib/hub-mapping.ts`),
`ProfessionalWire.email` + `ProfessionalConfigUpdatePayload.email` (`lib/secretaria-hub.ts`).

`ProfessionalWire.email` é **opcional** no tipo: um backend anterior a esta feature não
manda a chave, e `undefined` significa "esse backend não sabe me dizer", nunca "o médico
não tem endereço" — mesma regra dos flags `*_inherited`/`calendar_source`.

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

`tests/test_professional_config_gap_alert.py` (21 casos), nos dois níveis da skill
`conversation-flow-state`:

- **Router puro:** gap de horário dispara com `action`/`gap` certos e **sem tocar o
  calendário** (`day_scans == 0`); override próprio `{}` também é gap; **regressão** — agenda
  cheia continua no `NO_AVAILABLE_DAYS_MESSAGE` e chega a consultar o calendário;
  **regressão** — profissional completo continua chegando no day picker; multi-médico julga
  só o selecionado; gap de serviço mantém as bolhas de sempre.
- **Call site (sqlite in-memory + Redis fake):** os dois endereços recebem; as 6 combinações
  de destinatário (ambos / só um / nenhum / whitespace / duplicado); nenhum destinatário não
  levanta e o paciente é respondido do mesmo jeito; **um segundo paciente no mesmo
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

- [ ] Commit + push.
- [ ] **Deploy do `secretaria-worker`** — obrigatório e separado da API (o handler novo
      roda no worker; ver a regra de deploy no `CLAUDE.md` deste repo). Confirmar por
      `GET /build` / `source_fingerprint`, não só `deploy_parity: match`.
- [ ] Rodar a migração `f3a9c1d7b2e4` em produção.
- [ ] Deploy do `secretarIA-frontend` (campo de e-mail).
- [ ] `SMTP_HOST` precisa estar configurado no EasyPanel pro alerta sair — sem ele a
      função é um no-op silencioso, por design.
- [ ] `FEAT 42` (banner nos frontends) — ler §6 acima antes de começar.
