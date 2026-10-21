# TASK-054 — Boas-vindas cifradas e profissionais

Integra??o e push para `origin/main` autorizados pelo usu?rio em 2026-10-12. Implementa??o validada nos worktrees TASK-054; deploy e migra??o remota ficam a cargo do usu?rio e n?o foram executados pelo agente.

Leitura interna `/internal/tenants/{tenant_id}/professionals` inclui inativos, sem mudar a completude dos ativos. `ProfessionalAttachIn.interactive` é aditivo, default false. `services/professional_capacity.py` centraliza o gate usado pelo hub e criações interativas; bootstrap mantém seu contrato. As exceções de domínio são traduzidas na camada HTTP.

O hub valida actor/acesso ao vivo em cada requisição e memoiza somente no `request.state`, sem prolongar sessão revogada por cache positivo. Chamadas avulsas sem memo revalidam de modo fresco.

`staff_temporary_password` é um template novo. `services/staff_welcome.py` cifra destinatário, senha e variáveis antes de enfileirar. Somente `payload_encrypted` entra como kwarg no job `send_staff_welcome_email`, protegido pela redação ARQ existente. Job registrado em `WorkerSettings`; plaintext fica apenas em memória de transporte/worker e no e-mail necessário ao destinatário, nunca em logs/resultados.

Antes do envio, o worker consulta usuário, geração e prazo no brain-api. Callback `sent/failed` é versionado; reenvio cria nova senha/geração. Falha de fila/SMTP não é entrega concluída. Não há armazenamento durável plaintext nem promessa de SMTP exatamente uma vez.

Suíte completa final: **5033 passed, 68 skipped**, com warnings já existentes. As 14 falhas da primeira varredura eram fixtures sem `Request`/novo parâmetro `fresh` e expectativas antigas da lista de jobs; foram corrigidas e a suíte completa reexecutada. Sintaxe, diff e formatação verificados. Ruff está bloqueado pelo Windows.

PostgreSQL 16, Redis 7, worker real e SMTP capturador locais comprovaram entrega, callback, falha/reenvio e quatro perfis. Não houve e-mail externo. Credenciais e chave de cifra do teste foram geradas em memória e não constam nos relatórios.

API e worker precisam receber a mesma versão antes de habilitar os convites novos. A cifra existente deve funcionar em ambos; o brain-api precisa oferecer callbacks antes de emitir jobs. Deploy não autorizado.
