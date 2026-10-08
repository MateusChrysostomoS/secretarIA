# CHECKPOINT — remediação lembretes R6 (e-mail ao médico + entrada pelo link)

Estado: implementado e validado localmente (TASK-032). Deploy NOT AUTHORIZED ao agente.

## Causas tratadas
- Aviso ao médico: o gate usava o canal WhatsApp mesmo para atendimento Portal; com WhatsApp
  desligado, a edição gravava e enfileirava mas o job enviava zero. Agora o gate segue o canal do
  paciente. A remarcação legada (`appointment_reschedule`) passou a registrar o mesmo aviso.
  Motivos de descarte agora são logados. Não está provado que este foi o motivo da ocorrência do dono.
- Perda entre commit e enfileirar: intenção durável (`professional_edit_notices`, migração
  `c2d5f8a1e4b6`) + dispatcher com lease; revisão monotônica evita aviso obsoleto (A→B→A).
  Limite: SMTP aceito + queda antes do claim final pode duplicar (não é exactly-once).
- Entrada pelo link: link novo carrega `lembrete=<uuid opaco>`; `entry_context` aditivo
  (`navigation|clinic_link|reminder_link`). **Somente `reminder_link`** ignora silêncio/fluxo; a
  autorização (tenant, paciente autenticado, consulta própria) é revalidada no servidor.
  `clinic_link` é enviado a todo carregamento de URL e mantém as guardas normais (achado C1 da revisão).
- Cartão de lembrete não zera mais o rascunho de edição.

## Ordem de publicação (obrigatória)
migração `c2d5f8a1e4b6` → secretarIA worker e API → brain-api → frontend.
Código antes da migração faz toda edição R6 falhar (registro do aviso na mesma transação).

## Pendências da revisão (results/remediation-review.md)
- I1 causa em produção não provada: ler os novos motivos de descarte nos logs após o deploy.
- I2 fallback `clinic_link` sem `patient_name` no primeiro contato; I5 "Alterar Dados" recusado com
  rascunho de outra consulta (O08). Não corrigidos nesta rodada.
- I4 outbox sem prova de perda real: decisão do dono. D13/D16 (Particular/serviço) falharam em produção e não foram explicados.
- QA real (inbox, cron, clique) não executada; matriz: nenhum caso R como PASS.
