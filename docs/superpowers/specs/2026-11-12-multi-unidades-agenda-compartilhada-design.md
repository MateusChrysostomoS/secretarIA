# Multi-unidades + Agenda compartilhada por profissional — design (TASK-083)

Data: 2026-11-12. Repos: secretarIA (núcleo), brain-api (1 rota interna), Brain-Message-Frontend (telas
`/contexto` e `/agenda`; é o portal operacional — o secretarIA-frontend virou site de marketing).
Decisões tomadas sem perguntar ao dono, a pedido dele ("execute tudo sem pedir nada"); cada uma está
marcada **[decisão]** e é reversível sem retrabalho grande.

## 1. Por quê

1. **Multi-unidades.** Um médico que contrata a secretarIA só como assistente pessoal trabalha em mais de
   um lugar. Hoje `Unit` é só um endereço do tenant: nada liga unidade a profissional, a horário ou ao
   fluxo do paciente (`Appointment.unit_id` é o único uso e o fluxo determinístico nunca o preenche).
2. **Dupla reserva entre secretarIAs.** O mesmo médico pode ter a secretarIA pessoal (tenant P) e atender
   numa clínica que também usa (tenant C). Hoje a "agenda" é a chave `(tenant_id, professional_id)`: os dois
   tenants têm linhas `professionals` diferentes para a mesma pessoa e nunca se enxergam. Um paciente marca
   terça 10h pela P, outro terça 10h pela C — as duas confirmam.

## 2. Visão geral

- Nova entidade **`Agenda`** = o tempo de UMA pessoa. Vários `professionals` (de tenants diferentes)
  apontam para a mesma agenda (`professionals.agenda_id`).
- A **identidade da pessoa** vem do brain-api (autoridade de identidade): `StaffAccount.id`
  (`account_id`). Duas linhas `professionals` só compartilham agenda se o brain-api afirmar que são a mesma
  conta. **Nunca por e-mail digitado nem por nome.**
- Todo "horário livre" e toda gravação de consulta consideram o **ocupado dos profissionais irmãos** da
  mesma agenda (outros tenants), expondo só início/fim.
- Nova tabela **`professional_units`**: em quais unidades o profissional atende e com que horário em cada
  uma. O paciente escolhe a unidade quando o médico atende em mais de uma; o endereço aparece na
  confirmação, no lembrete e no evento do calendário.

## 3. Goals / non-goals / anti-goals

Goals
- Impossível confirmar dois pacientes sobrepostos para a mesma pessoa, mesmo em tenants diferentes, nos
  dois backends (local `calendar_events` e Google) e em todos os caminhos de escrita do paciente.
- Várias unidades por profissional, com horários por unidade.
- Migração aditiva; sem `agenda_id` o comportamento é o de hoje, byte a byte.
- Nenhum vazamento entre tenants: o irmão só contribui com janelas `(início, fim)`.

Non-goals
- Tempo de deslocamento entre unidades **[decisão: fora; futuro `Agenda.travel_buffer_minutes`]**.
- Ler o Google Calendar de OUTRO tenant (exigiria usar o token de outro tenant). Limitação documentada: com
  Google em calendários diferentes, o outro tenant enxerga as **consultas** (`appointments`) e bloqueios
  locais do irmão, mas não eventos pessoais soltos do Google do irmão. Para cobertura total o médico conecta
  o mesmo calendário nos dois (já funciona por `references_same_calendar`) ou usa a agenda nativa.
- Billing/Stripe: `multi_unit` e `limits["units"]` ficam como estão.
- Compartilhar pacientes/serviços/preços entre tenants.

Anti-goals: um tenant ver paciente, serviço ou nome de clínica do outro; ligar agendas por palpite.

## 4. Abordagens

A. **Agenda cross-tenant no banco do secretarIA** (`agendas` + `professionals.agenda_id`), ocupado dos
   irmãos unido na leitura e no lock de escrita. (**escolhida**) Funciona para local e Google; tabela nova +
   colunas nulas; reaproveita `extra_busy`/holds. Custo: 1 rota nova no brain-api.
B. Tabela global de profissionais sem `tenant_id`. Reescreve FKs/privacidade em todo lugar. Rejeitada.
C. Google Calendar único como verdade. Contradiz a decisão de agenda nativa (2026-10-20). Rejeitada.

## 5. Modelo de dados (secretarIA)

```
agendas(id uuid pk, person_key varchar(64) UNIQUE NOT NULL  -- StaffAccount.id, created_at)
professionals  + agenda_id uuid NULL FK agendas.id ON DELETE SET NULL (index)
professional_units(professional_id FK CASCADE, unit_id FK CASCADE,
                   business_hours json NULL  -- formato de professionals.business_hours; NULL = herda
                   is_active bool default true, sort_order int default 0, created_at,
                   PK(professional_id, unit_id))
calendar_events + unit_id uuid NULL (sem FK: rótulo histórico)
booking_holds   + unit_id uuid NULL
```
`Appointment.unit_id` já existe. **[decisão]** Com ≥1 vínculo ativo, a disponibilidade de uma unidade =
horário do vínculo (ou herdado). Sem vínculos = agenda sem unidade (hoje). O ocupado é sempre da agenda
inteira: a pessoa está num lugar por vez. `Unit` continua por tenant; só se vincula unidade do próprio tenant.

## 6. Ligação das agendas

- brain-api: `GET /internal/tenants/{tenant_id}/professional-accounts` → `[{professional_id, account_id}]`
  (mesmo gate e regra de `professional-emails`; só profissionais com `User` vinculado).
- secretarIA `services/agenda_linking.py`: `ensure_agenda_for_professional` (get_or_create
  `Agenda(person_key=account_id)`, seta `professional.agenda_id`, idempotente) e `sync_tenant_agendas`
  (1 chamada ao brain-api). Falha do brain-api nunca quebra o fluxo: sem `agenda_id` = hoje.
- Gatilhos: criação/attach de profissional (`api/internal_provisioning.py`); `POST /tenants/me/agendas/sync`
  (gestor); `scripts/reconcile_agendas.py` (backfill pós-deploy).
- O hub nunca aceita `agenda_id`/`person_key` do cliente.

## 7. Ocupado compartilhado e escrita sem dupla reserva

`services/agenda.py`:
- `sibling_professional_ids(session, professional_id)`; `shared_busy(session, professional_id, start, end)
  -> list[BusyWindow]` = união de appointments vivos dos irmãos, `calendar_events` locais ocupados dos irmãos
  e `booking_holds` fase `payment` dos irmãos — **só janelas**; `agenda_write_lock` =
  `pg_advisory_xact_lock(hash("agenda:"+agenda_id))` (no-op em SQLite).

Integração (seams da recon):
1. `CalendarService`: `shared_busy_provider` opcional, injetado em `from_tenant_config` /
   `tenant_config.resolve_professional_calendar` quando há `agenda_id`; entra em `check_availability`,
   `has_conflict`, `is_slot_free`, `list_free_slots(extra_busy)` → slots, `slot_taken_at_confirm`, IA,
   promoção de hold e Pix passam a ver os irmãos sem mudar cada chamador.
2. `LocalCalendarService`: `agenda_write_lock` usa a chave da agenda quando existe; `_overlap_exists`
   inclui `shared_busy` → `create/update_event_exclusive` atômicos entre tenants.
3. Google **[decisão]**: `exclusive_write` genérico em `CalendarService` quando há `agenda_id` (lock da
   agenda + reconfere `shared_busy` + `check_availability` + cria). Os `isinstance(LocalCalendarService)`
   que guardam o caminho exclusivo viram `calendar.supports_exclusive_write`.
4. Hub da equipe (`api/hub/calendar.py`, `staff_appointment_edit.py`) segue permissivo (encaixe), mas
   responde `conflicts_other_agenda: true` quando colide com irmão **[decisão: avisa, não bloqueia]**.
5. `release_keep._consultation_overlaps` e `payments/service_change` incluem appointments dos irmãos.
6. Privacidade: nunca summary, paciente ou tenant do irmão, nem em log.

Erro de banco no ocupado compartilhado propaga (mesmo banco). Só a chamada ao brain-api é tolerante.

## 8. Unidades no fluxo do paciente

- Estado `AWAITING_UNIT` (padrão do passo de convênio/profissional): com >1 vínculo ativo o paciente
  escolhe a unidade (nome + endereço curto); com 1, pula; sem vínculos, fluxo de hoje.
- Dia/horário usam o horário da unidade escolhida ∩ ocupado da agenda inteira.
- `unit_id` gravado em Appointment/CalendarEvent/BookingHold; evento (Google/local) com `location` =
  nome + endereço; confirmação, lembrete e e-mails mostram a unidade (hoje `draft_resolution` zera o
  endereço quando há unidades — corrigir).
- IA: `list_units`/`create_event_at_unit` respeitam `professional_units`; reagendar mantém a unidade.

## 9. API do Portal (hub)

- `GET/PUT /tenants/me/professionals/{id}/units` (gestor ou o próprio médico; valida unidade do tenant;
  entitlement/limite como `units.py`).
- `GET /tenants/me/professionals/{id}/agenda` → `{shared, other_tenants_count}` (sem nomes).
- `POST /tenants/me/agendas/sync` (gestor). `GET /tenants/me/units` ganha `professionals_count`.

## 10. Telas (Brain-Message-Frontend)

`/contexto`: seção "Unidades" (nova, ids em `SectionId`/`TOC_LABELS`), "Locais de atendimento" no
`ProfessionalRow` com horário por unidade (reaproveita `AvailabilitySection`), selo "Agenda compartilhada"
+ botão "Sincronizar agenda". `/agenda`: unidade/endereço no `Drawer`, `NewApptModal` com seletor de unidade
e aviso de conflito com outra agenda. Textos pt-BR sem jargão técnico.

## 11. Riscos

| Risco | Cobertura |
|---|---|
| Regressão sem agenda | `agenda_id` NULL ⇒ caminhos novos viram no-op; caracterização antes de mexer |
| Dupla reserva concorrente | teste real em Postgres (docker) + lock da agenda |
| Vazamento entre tenants | testes: só janelas; hub não expõe nomes |
| `isinstance(Local)` espalhado | trocar por capacidade num commit, teste por caminho |
| Suíte estoura memória | rodar por grupos; falha pré-existente ≠ regressão |
| brain-api fora | ligação tolerante; script/botão de reconciliação |

## 12. Deploy (informativo; esta tarefa não deploya)

Migração do secretarIA antes de API+worker; rota do brain-api antes (a ligação tolera ausência); depois o
front. Rodar `scripts/reconcile_agendas.py` uma vez. Só commit local; push/deploy a pedido do dono.
