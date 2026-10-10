# CHECKPOINT — "Meus pacientes" no console (TASK-046 R8, parte A)

Spec: `docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md` §5.D (decisão do dono de
2026-10-09 que substitui a tabela anterior). Plano: `docs/superpowers/plans/2026-10-09-lembretes-r8-filtro-meus-pacientes-e-seletor-medico.md`.

## Estado
- Local, commitado na branch `task/TASK-046-meus-pacientes-api` (worktree `BRAIN-worktrees/TASK-046/secretarIA`),
  sobre `origin/main` cb526d5 (R7 já pushado ao GitHub em 2026-10-10). Esta branch não foi mesclada, pushada nem
  deployada. Sem migração.
- Suíte completa: 4926 passed, 68 skipped, 0 failed (linha de base pré-R8 em cb526d5: 4913 passed, 68 skipped).

## O que mudou
- `GET /tenants/me/conversations?mine=true` (`api/hub/conversations.py::list_conversations`): só conversas cujo
  paciente tem/teve consulta com o profissional do próprio usuário. Sem o parâmetro: lista de sempre, para todos
  (inclusive o médico restrito da agenda) — filtro, nunca permissão.
- Regra única em `services/doctor_patients.py::is_patient_of` + `models/appointment.py::DOCTOR_PATIENT_STATUSES`
  (agendada, confirmada, remarcada, compareceu, faltou; **cancelada não conta**; casa por paciente, mesmo tenant).
- Usuário sem profissional próprio + `mine=true` → 422 `no_own_agenda` (mesmo código do `GET /calendar/events?mine=true`).
- O papel/profissional vem do brain-api via `api/hub/deps.py::get_agenda_viewer` (R7); nenhuma mudança no brain-api.

## Deploy (não executado)
`secretaria_api` antes do Brain-Message-Frontend (parte B). O worker não muda; deployar junto só mantém
`deploy_parity=match`. Front antes da API = o filtro mostraria todas as conversas (a API velha ignora `mine`).

## Fora do escopo
- Nenhuma rota `/{id}/…` de conversa mudou; o console continua sem escopo por papel.
- `brain-api/docs/PORTAL_MESSAGING_API.md` não descreve a lista do hub (só a cita); não foi alterado.
