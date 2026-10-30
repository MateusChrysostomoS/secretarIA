# Medições do Asaas real (TASK-071, Plano 4)

Regras do Asaas que **não estão na documentação oficial** só entram no código depois de medidas
aqui, com data e resposta observada (só números e verdadeiro/falso, nunca texto do Asaas, chave,
CPF ou ids).

## Como medir

O dono roda no próprio terminal (a chave e o CPF/CNPJ são pedidos por `getpass`, não aparecem):

```
! uv run python scripts/asaas_measure.py
```

O script cria um cliente de teste e cobranças Pix de R$ 1,00 / 4,99 / 5,00 / 9,99 / 10,00,
consulta o QR de cada uma e apaga tudo. Cada passo imprime uma linha JSON (`step`, `status`,
`code`, `kw_value`, `kw_pix_key`, `kw_account`, `kw_document`, `qr_has_pix_code`, estados do
cadastro). Cole as linhas na seção abaixo.

## Resultado

**PENDENTE** — ainda não rodado pelo dono. Até lá:

- `MIN_PIX_CENTS` (valor mínimo do Pix) **não existe** no código; o item `amount_below_minimum`
  do checklist fica desligado.
- O "Testar cobrança" usa R$ 5,00 (`PROBE_AMOUNT_CENTS = 500`), o piso de cobrança do Asaas
  conforme a documentação pública; revisar quando a medição chegar.
- As palavras-chave de `_classify_text` (valor, chave Pix, conta) continuam só as que já existem.

### Relatório (a preencher)

| Data | Passo | status | code | kw_value | kw_pix_key | kw_account | kw_document | qr_has_pix_code |
|---|---|---|---|---|---|---|---|---|

### Perguntas que o relatório responde

- Menor valor Pix aceito.
- `pix_key_missing` / `account_not_enabled` aparecem na criação da cobrança ou só no QR?
- Quais estados de `/myAccount/status` coexistem com cobrança funcionando (conta com documentos
  pendentes).
- Um segundo cliente com o mesmo CPF/CNPJ volta com o MESMO id (`customer_again.same_customer_id`)? Se sim, o "Testar cobrança" pode cair num cliente existente: o código já não apaga um cliente guardado para paciente e pede ao Asaas para não notificar o cliente de teste, mas a medição decide se isso basta.
- Cobrança apagada gera tarifa? (conferir no extrato do Asaas depois da medição.)
