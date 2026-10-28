"""Payment-related services: the Pix deposit (sinal) lifecycle.

`money.py` (BRL free-text parsing/formatting), `asaas.py` (the Asaas PSP
client) and `deposit_lifecycle.py` (every state transition + patient-facing
copy) together implement the REAL Pix deposit charge — see
`deposit_lifecycle.py`'s module docstring for the full picture. The old
message-only `pix.py` stub (no real charge, just a rendered WhatsApp message
embedding `Tenant.pix_key`) has been deleted; `plugins/pix_deposit.py` is its
replacement — a core SecretarIA capability since 2026-10-28, no longer gated by
the `pix_deposit` entitlement key (docs/CHECKPOINT_sinal_padrao.md).
"""
