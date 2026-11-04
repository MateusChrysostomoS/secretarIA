# CHECKPOINT — e-mail operacional da clínica (`Tenant.contact_email`)

Estado: **construído localmente, NÃO commitado, NÃO deployado.** Sem migração (a coluna
`tenants.contact_email`, `String(254)`, nullable, já existe e já recebe os alertas de pagamento,
agendamento e atendimento humano). Esta entrega só deixa o e-mail ser **lido e alterado**.

## O que entrou onde

| Peça | Onde |
|---|---|
| Schemas `ContactEmailUpdate` / `ContactEmailRead` + normalização | `src/secretaria/schemas/contact_email.py` |
| Escrita compartilhada (hub + interno), sem commit, log sem o endereço | `src/secretaria/services/contact_email.py::set_contact_email` |
| `PATCH /tenants/me/contact-email` | `src/secretaria/api/hub/config.py::update_contact_email` |
| `contact_email` em `GET /tenants/me/config` (somente leitura) | `schemas/config.py::TenantConfigRead` + `services/hub_configuration.py::tenant_read_model` |
| `GET`/`PATCH /internal/tenants/{tenant_id}/contact-email` | `src/secretaria/api/internal_provisioning.py` |

`TenantConfigUpdate` **não** ganhou o campo: o PATCH dedicado é o único escritor. `PUT
/tenants/me/config` e `PUT /tenants/me/configuration` ignoram um `contact_email` enviado (coberto por
teste).

## Contrato

Normalização (só no `PATCH`): `strip` → minúsculas → string vazia vira `null` (limpa) → no máximo 254
caracteres → sintaxe estrita de e-mail (regex; `email-validator` NÃO é dependência do projeto), senão
**422**. A chave `contact_email` é **obrigatória**: corpo `{}` é 422 (um erro de digitação nunca limpa o
e-mail em silêncio). `null` explícito limpa.

```jsonc
// PATCH /tenants/me/contact-email            (Bearer do hub; qualquer usuário autenticado da clínica)
// PATCH /internal/tenants/{tenant_id}/contact-email   (X-Internal-Api-Key)
{ "contact_email": " Foo@Bar.COM " }          // -> 200 { "contact_email": "foo@bar.com" }
{ "contact_email": null }                      // -> 200 { "contact_email": null }
{ "contact_email": "" }                        // -> 200 { "contact_email": null }
{ "contact_email": "nao-e-email" }             // -> 422
{}                                             // -> 422

// GET /internal/tenants/{tenant_id}/contact-email
// -> 200 { "contact_email": "clinica@exemplo.com" }   (ou null)

// GET /tenants/me/config  -> { ..., "contact_email": "clinica@exemplo.com" | null, ... }
```

Códigos: hub — 200, 401 (sem token / token inativo), 404 (token válido sem tenant), 422. Interno — 200,
401 (chave ausente/errada), 403 (`INTERNAL_API_KEY` não configurada), 404 `Unknown tenant`, 422.
O hub **não** tem porteiro de papel (mesma confiança dos campos não-financeiros do `PUT /config`).

Isolamento: o hub escreve sempre no tenant do token; a rota interna só no tenant do path.

Log: evento `contact_email_updated` com `tenant_id`, `set` e `cleared` (booleanos). **Nunca** o
endereço (PII). O erro 422 não ecoa o valor em `str(ValidationError)` (`hide_input_in_errors`); o corpo
da resposta 422 do FastAPI devolve ao chamador o próprio `input`, como em qualquer 422.

## Testes

- `tests/test_contact_email_schema.py` — normalização, inválidos, 254, chave obrigatória.
- `tests/test_hub_contact_email.py` — auth (401), set, normalização, limpar com `null`/`""`/espaços,
  422, chave ausente, qualquer papel, isolamento entre tenants, log sem o e-mail, `GET /config`, `PUT
  /config` e `PUT /configuration` não alteram.
- `tests/test_internal_contact_email.py` — 401/403, GET, PATCH (set/limpar/422), 404 tenant
  desconhecido, só o tenant do path muda.

Comando: `BOT_ALLOWLIST_WA_IDS="" uv run python -m pytest tests/test_contact_email_schema.py
tests/test_hub_contact_email.py tests/test_internal_contact_email.py`.

## Deploy

Sem migração. **Só a API** (`secretaria_api`) muda — o worker não é tocado (não há código em
`workers/` nem `flow_router.py`); como o `contact_email` é lido em runtime pelos alertas, nada a
reiniciar no worker. Ordem: **secretarIA → brain-api → frontends.** Push e deploy dependem do pedido do
dono.
