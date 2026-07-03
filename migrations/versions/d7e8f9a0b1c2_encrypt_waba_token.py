"""Encrypt WhatsApp token at rest: tenants.access_token -> waba_token_encrypted

The last plaintext tenant secret leaves the schema. Existing values are Fernet-
encrypted (core.crypto / ENCRYPTION_KEY) into the credentials table, then the
plaintext column is DROPPED. If any tenant still carries a plaintext token and
ENCRYPTION_KEY is not configured, the migration FAILS LOUDLY rather than silently
discarding a live credential.

No plaintext value is ever printed/logged by this migration.

Revision ID: d7e8f9a0b1c2
Revises: b1c2d3e4f5a6
Create Date: 2026-07-02 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7e8f9a0b1c2"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenant_credentials",
        sa.Column("waba_token_encrypted", sa.Text(), nullable=True),
    )

    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, access_token FROM tenants "
            "WHERE access_token IS NOT NULL AND access_token != ''"
        )
    ).fetchall()

    if rows:
        # Import inside the migration so an empty database never needs the key.
        from secretaria.core.crypto import EncryptionError, encrypt

        try:
            encrypted_by_tenant = {tenant_id: encrypt(token) for tenant_id, token in rows}
        except EncryptionError as exc:
            raise RuntimeError(
                "tenants.access_token holds live WhatsApp credentials but they cannot "
                "be encrypted (is ENCRYPTION_KEY set?). Refusing to drop the column and "
                "lose them. Configure ENCRYPTION_KEY and re-run the migration."
            ) from exc

        for tenant_id, ciphertext in encrypted_by_tenant.items():
            existing = conn.execute(
                sa.text("SELECT id FROM tenant_credentials WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            ).fetchone()
            if existing:
                conn.execute(
                    sa.text(
                        "UPDATE tenant_credentials SET waba_token_encrypted = :ct, "
                        "updated_at = now() WHERE tenant_id = :tid"
                    ),
                    {"ct": ciphertext, "tid": tenant_id},
                )
            else:
                conn.execute(
                    sa.text(
                        "INSERT INTO tenant_credentials "
                        "(id, tenant_id, waba_token_encrypted, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), :tid, :ct, now(), now())"
                    ),
                    {"ct": ciphertext, "tid": tenant_id},
                )

    op.drop_column("tenants", "access_token")


def downgrade() -> None:
    # Re-create the plaintext column and restore values best-effort (symmetric key,
    # so a decrypt-back is possible while ENCRYPTION_KEY is unchanged). A failed
    # decrypt leaves '' rather than blocking the rollback.
    op.add_column(
        "tenants",
        sa.Column("access_token", sa.String(length=512), nullable=False, server_default=""),
    )

    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT tenant_id, waba_token_encrypted FROM tenant_credentials "
            "WHERE waba_token_encrypted IS NOT NULL"
        )
    ).fetchall()
    if rows:
        from secretaria.core.crypto import EncryptionError, decrypt

        for tenant_id, ciphertext in rows:
            try:
                plaintext = decrypt(ciphertext)
            except EncryptionError:
                continue  # unrecoverable without the original key; leave ''
            conn.execute(
                sa.text("UPDATE tenants SET access_token = :tok WHERE id = :tid"),
                {"tok": plaintext, "tid": tenant_id},
            )

    op.drop_column("tenant_credentials", "waba_token_encrypted")
