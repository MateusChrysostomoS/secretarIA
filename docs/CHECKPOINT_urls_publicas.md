# TASK-055 — Base pública única do Brain-Message

Implementação validada; commit e push para `origin/main` autorizados pelo usuário na TASK-055. Sem deploy manual. Configurar `BRAIN_MESSAGE_PUBLIC_URL` na API **e** no worker; usar a raiz do portal sem caminho. Agenda, retorno Google e convites de pacientes são derivados como `/agenda`, `/contexto` e `/clinicas/`.

Essa entrada substitui `DOCTOR_AGENDA_URL`, `PORTAL_POST_OAUTH_REDIRECT` e `BRAIN_MESSAGE_PORTAL_URL`. Os nomes calculados de agenda e retorno Google continuam disponíveis no objeto Settings, sem leitura das variáveis antigas.

Guia completo, nomes por serviço, ordem de publicação e validação: [URLs públicas](../../brain-api/docs/CHECKPOINT_urls_publicas.md). Os valores antigos só devem ser retirados quando o serviço usar a versão nova. `GOOGLE_OAUTH_REDIRECT_URI` continua sendo o callback registrado no Google.
