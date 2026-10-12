# Limpeza administrativa de pacientes — TASK-029

Integrado à main local em 2026-10-03; sem push, publicação ou execução em produção.

Rotas internas: `GET /internal/tenants/{tenant_id}/patient-cleanup/preview` e `POST /internal/tenants/{tenant_id}/patient-cleanup`. Autenticação existente `X-Internal-Api-Key` por `require_internal_api_key`; operação pública é orquestrada pelo Brain com login admin. POST exige `confirm=true` booleano e `clinic_name` exato.

`services.tenant_patient_cleanup.cleanup_patients` remove todos os pacientes, conversas, mensagens, ConversationPiiTokenMap, consentimentos, BookingHold e RebookingDecline do tenant. Remove objetos R2 referenciados pelas mensagens antes de apagar as referências, validando o prefixo tenant/paciente. Falha de storage ou referência inconsistente entre tenants bloqueia/falha sem ocultar o impedimento. A operação pode ser repetida.

Preserva clínica, equipe, configuração, billing, agenda e registros PixDeposit. Desvincula PixDeposit.patient_id; anonimiza patient_id/conversation_id/phone/attendee_name em Appointment, inclusive registros antigos já sem vínculo de paciente. Mantém datas, status, IDs e eventos externos. Google Calendar e Asaas não são chamados; dados pessoais existentes nesses serviços permanecem. Audit trail AnalyticsEvent contém somente contagens da limpeza.

Prévia retorna contagens, impedimentos e avisos sem PII; não escreve. Resultados `ready/completed/not_provisioned/blocked/failed`. Não há nova variável, migration ou mudança em workflow. Função aditiva `media_storage.delete_object_required` propaga falhas de exclusão; a função anterior de melhor esforço permanece disponível aos seus callers.

Operar com intake parado: o lock do tenant não impede todas as fontes concorrentes de escrita. Backup/recuperação e compatibilidade dos três serviços devem ser verificados pelo operador. Guia Swagger completo: brain-api/docs/CHECKPOINT_tenant_patient_cleanup.md. Limpar históricos não corrige a criação de conversas em novos logins.

Validação: cinco testes novos com SQLite/FKs, incluindo isolamento, idempotência, R2 com falha, guarda interna e anonimização da agenda legada; suíte relacionada 23 passed. Sem validação de infraestrutura ou banco PostgreSQL real. Resultado completo e baseline em TASK-029/REVIEW.md.
