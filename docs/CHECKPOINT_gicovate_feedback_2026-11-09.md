# CHECKPOINT — Instituto Gicovate, feedback de 2026-11-09

Áudio do Dr. Carlos (primeiro cliente real) depois de testar o Portal. Três pontos, três correções.
Branch `fix/gicovate-feedback-1109` (secretarIA) + `fix/convenio-ordem` (Brain-Message-Frontend).
**Local, não commitado, não pushado, não deployado.**

## 1. Boas-vindas confusa

Antes: "Bem-vindo(a) **à** Instituto Gicovate! Sou a SecretarIA, assistente virtual **do consultório do**
Instituto Gicovate, cirurgia do aparelho digestivo …, em Campos dos Goytacazes, **em Campos dos Goytacazes**."

Agora (`services/greeting_template.py`):
- artigo pelo primeiro nome: Instituto/Consultório/Centro/Espaço/Hospital/… → "ao"/"do"; o resto
  (Clínica, nome de marca) segue "à"/"da", como sempre foi;
- sai "do consultório do";
- a cidade não é repetida quando a descrição já a cita (comparação sem acento/maiúscula);
- a frase sempre termina com ponto;
- no Portal, o nome do paciente vai para o "Olá, Maria!" (antes ficava "… à Clínica, Maria!").

O orçamento de 1024 caracteres continua exato (`clinic_description_budget` mede com a sonda).
O preview do hub vem do backend (`greeting_preview_template`), não precisa de front.

## 2. Preço particular aparecia com convênio

"Escolhi Unimed e Primeira Consulta, apareceu R$200; Unimed não tem valor."
`booking_details.service_price_text(service, insurance)`: com convênio (qualquer resposta que não seja
"Particular" nem vazia, inclusive "Outro convênio" digitado) o preço não aparece. Pontos cobertos:
- cartão do serviço (`flow_router._service_detail_text`) → "Primeira Consulta — convênio Unimed";
- resumo "Confira os detalhes" (`booking_details_text`);
- saudação "você já tem consulta marcada" (`workers/shared/greeting.py`).

Fora do escopo: a lista de serviços que a IA recebe ainda mostra preço (se o paciente perguntar
"quanto custa", a IA responde o particular). O sinal Pix já tem a chave por convênio
(`charge_deposit`); conferir que Unimed do Gicovate está com "não cobra sinal" se o Pix for ligado.

## 3. Ordem dos convênios

Unimed caía na 2ª página ("Ver mais convênios") por ordem alfabética.
- migração `d2f6b8a4c1e7`: `tenant_insurance_plans.sort_order INTEGER NULL`;
- `list_tenant_plans` ordena: posicionados por `sort_order`, depois o resto em ordem alfabética
  (o fluxo do paciente usa essa mesma lista, então a 1ª página respeita a escolha);
- `PUT /tenants/me/insurance-plans/order {plan_ids}` (404 se um id não é da clínica);
- `GET /tenants/me/insurance-plans` devolve `sort_order` e já vem na ordem;
- modo `independent` continua alfabético.

## Deploy (ordem)

1. `alembic upgrade head` (nova imagem; serviços antigos não leem a coluna).
   Atenção: se `fix/lembretes-semana-dia` (migração `c4e8a1d7f3b6`) entrar antes, rebasear esta
   migração para ficar depois dela (dois heads senão).
2. `secretaria_api` + `secretaria-worker` juntos (`GET /build` → `deploy_parity: match`).
3. Brain-Message-Frontend (setas na tela de convênios).
4. No Gicovate: colocar Unimed em 1º e refazer o teste do áudio (boas-vindas, Unimed → Primeira Consulta).
