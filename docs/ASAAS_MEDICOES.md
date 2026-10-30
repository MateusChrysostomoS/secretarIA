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

## Resultado — rodado pelo dono em 2026-10-30, conta real (produção)

Relatório (só números e verdadeiro/falso):

| Passo | status | code | observação |
|---|---|---|---|
| `account_status` | 200 | — | general APPROVED, documents APPROVED, commercial APPROVED, **bank PENDING** |
| `customer` | 200 | — | cliente de teste criado |
| `customer_again` (mesmo CPF/CNPJ) | 200 | — | `same_customer_id: false` |
| `charge_100` (R$ 1,00) | **400** | invalid_object | `kw_value: true` (o texto fala de valor) |
| `charge_499` (R$ 4,99) | **400** | invalid_object | `kw_value: true` |
| `charge_500` (R$ 5,00) | 200 | — | QR com código Pix: **sim** |
| `charge_999` (R$ 9,99) | 200 | — | QR com código Pix: **sim** |
| `charge_1000` (R$ 10,00) | 200 | — | QR com código Pix: **sim** |
| todas as exclusões (cobranças e clientes) | 200 | — | tudo que o script criou foi apagado |

### Conclusões
1. **Valor mínimo do Pix = R$ 5,00.** Abaixo disso o Asaas recusa a cobrança com 400. No código: `money.ASAAS_MIN_PIX_CENTS = 500`; `readiness.MIN_PIX_CENTS` passou a valer (item `amount_below_minimum`); `AsaasProvider.create_pix_charge` marca um 400 abaixo de R$ 5,00 como `value_too_low`. O texto do Asaas **não** foi capturado (o script só guarda verdadeiro/falso), então a regra usa o valor enviado como prova, não palavras do texto; `_classify_text` continua só com "document".
2. **A chave de API lê `/myAccount/status`** (200): o item "cadastro concluído" é preenchido pelo sistema, não por clique.
3. **Cadastro incompleto não impede cobrar:** com `bank` PENDING, cobranças R$ 5,00+ foram criadas e devolveram código Pix. O estado do cadastro é só informação (aviso amarelo), nunca bloqueio; quem prova que cobra é a cobrança real.
4. **Esta conta devolve o código Pix** (`qr_has_pix_code: true`): há chave Pix (ou o Asaas gera o código sem ela). Os casos `pix_key_missing` / `account_not_enabled` **não** puderam ser provocados e continuam não medidos.
5. **O Asaas NÃO reaproveita cliente para o mesmo CPF/CNPJ** (`same_customer_id: false`, cria outro): o risco de o "Testar cobrança" cair num cliente existente não se confirma. As proteções (cliente de teste sem notificação; nunca apagar cliente guardado de paciente) ficam como defesa extra.
6. **Cobrança apagada e tarifa:** não dá para medir pelo script. Conferir no extrato do Asaas se as 3 cobranças apagadas (R$ 5,00 / 9,99 / 10,00) geraram tarifa.

### Pendente
- Capturar o texto real do 400 de valor mínimo (para palavras-chave em `_classify_text`): exigiria alterar o script para guardar o texto fora do chat; não necessário para o funcionamento atual.
- Medir o caso "sem chave Pix" numa conta de teste.
