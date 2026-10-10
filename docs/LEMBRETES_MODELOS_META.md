# Modelos do WhatsApp para os lembretes (TASK-032 R2)

Folha para submeter à Meta (WhatsApp Manager → Ferramentas da conta → Modelos de mensagem).
Modelos são aprovados **por conta do WhatsApp Business (WABA)**: submeta em cada WABA de clínica
que vai ligar o lembrete novo. A aprovação é externa (minutos a dias). Enquanto o modelo novo não
estiver aprovado, o sistema continua funcionando com o modelo simples de hoje (seção 2).

## 1. Modelo novo com 3 botões — `lembrete_consulta_v2`

| Campo | Valor |
|---|---|
| Nome | `lembrete_consulta_v2` |
| Categoria | Utilidade (Utility) |
| Idioma | Português (BR) — `pt_BR` |
| Cabeçalho | nenhum |
| Rodapé | nenhum |

Corpo (copiar exatamente, em uma única linha):

> LEMBRE-SE: {{1}} com {{2}} está marcada para o dia {{3}} às {{4}} para {{5}}. {{6}} Toque em um dos botões abaixo para responder.

Exemplos que a Meta pede para cada variável:

| Variável | O que entra | Exemplo para a Meta |
|---|---|---|
| `{{1}}` | de quem é a consulta | `Sua consulta` (consulta marcada para outra pessoa: `A consulta de João Pedro`) |
| `{{2}}` | médico(a) | `Dra. Ana Souza` (sem médico cadastrado: `a equipe da Clínica Olhar`) |
| `{{3}}` | data, no fuso da clínica | `10/10/2026` |
| `{{4}}` | hora, no fuso da clínica | `14:30` |
| `{{5}}` | serviço | `Consulta oftalmológica` |
| `{{6}}` | orientações do serviço, numa linha só | `Orientações: jejum de 8 horas; trazer exames anteriores.` (sem orientações: `Sem orientações especiais.`) |

Botões — tipo **Resposta rápida** (quick reply), **nesta ordem** (o sistema envia o código de
cada botão pela posição):

1. `Confirmar`
2. `Cancelar`
3. `Alterar Dados`

Desde 2026-10-07 (R6), o terceiro botão do cartão interativo chama-se **Alterar Dados** e
abre o menu de mudança da consulta. Se este modelo já tiver sido aprovado com **Outro**, o
botão continua funcionando: o código é enviado pela posição e já é o de Alterar Dados.
O rótulo na Meta só muda reenviando o modelo; enquanto isso, fora da janela de 24 h o paciente
pode continuar vendo **Outro**. Nenhum modelo foi submetido ou alterado nesta execução local.

O que o sistema já garante (não precisa configurar): nenhuma variável vai vazia, com quebra de
linha, tabulação ou mais de 4 espaços seguidos (regra da Meta); as orientações vêm achatadas numa
linha e cortadas em 400 caracteres; os rótulos dos botões têm no máximo 20 caracteres.

## 2. Modelos que já existem e continuam em uso

| Nome | Uso no lembrete novo |
|---|---|
| `appointment_reminder` | Modelo simples de hoje (1 variável, sem botões). Usado fora da janela de 24 h enquanto o modelo novo não está aprovado, se a Meta recusar o modelo novo numa WABA, e sempre que o paciente já confirmou duas vezes (lembrete sem pedido de confirmação). A variável recebe agora o texto novo numa linha: `LEMBRE-SE: Sua consulta com … às 14:30 para Consulta. Orientações: …` |
| `appointment_reminder_deposit` | Consulta com sinal Pix pago: 1 variável (o mesmo texto numa linha) + 3 respostas rápidas `Confirmar` / `Reagendar` / `Cancelar`, como hoje. |

## 3. Configuração e ativação (quem faz: o dono, no painel; nenhuma automação mexe em variáveis)

| Variável de ambiente (API **e** worker) | Valor | Quando |
|---|---|---|
| `REMINDER_V2_TEMPLATE_NAME` | `lembrete_consulta_v2` | só se o nome aprovado for outro |
| `REMINDER_V2_TEMPLATE_APPROVED` | `false` → `true` | trocar para `true` só depois da aprovação na(s) WABA(s) das clínicas ligadas |
| `BRAIN_MESSAGE_PORTAL_URL` | o mesmo valor que o brain-api usa | para o e-mail do Portal levar o link "abrir minha conversa" |

Enquanto `REMINDER_V2_TEMPLATE_APPROVED=false`: dentro da janela de 24 h o paciente recebe a
mensagem com os 3 botões normalmente; fora dela recebe o modelo simples (sem botões) e responde
pela conversa — a mensagem de abertura do chat (plano R3) mostra os botões quando ele escrever.


## R7 — avisos das ações da clínica (2026-10-09) — PROPOSTA, não submetida

Hoje (sem modelo aprovado) o aviso fora das 24 h vai pelo modelo de uma variável `REMINDER_TEMPLATE_NAME`, só texto. Para os botões funcionarem fora das 24 h seriam necessários dois modelos UTILITY (pt_BR), cada um com três respostas rápidas na ordem **Confirmar / Cancelar / Alterar Dados** (o payload é posicional: `remconfirm|<id>`, `remcancel|<id>`, `remedit|<id>`, o mesmo do lembrete):

| Nome proposto | Corpo | Variáveis |
|---|---|---|
| `clinica_confirmou_v1` | `Seu médico confirmou {{1}} de {{2}} às {{3}} com {{4}}. Você está ciente?` | 1 = "sua consulta" / "a consulta de <nome>", 2 = dd/mm/aaaa, 3 = HH:MM, 4 = médico |
| `clinica_alterou_v1` | `A clínica alterou {{1}}: {{2}}. Agora: {{3}} às {{4}} com {{5}}.` | 1 = como acima, 2 = mudanças numa linha ("Data: 16/10 → 17/10; Horário: 14:00 → 15:30"), 3–5 = como acima |

Pós-consulta e cancelamento não precisam de modelo novo (texto sem botões e `CANCEL_TEMPLATE_NAME`, respectivamente). Depois de aprovados, o código precisa de um ajuste pequeno em `services/staff_patient_message.py::_clinic_notice_whatsapp` (nome do modelo + variáveis + `button_payloads`, atrás de uma flag `*_APPROVED`, como o `REMINDER_V2_TEMPLATE_APPROVED`).
