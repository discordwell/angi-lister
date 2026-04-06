"""API keys table for tenant and admin authentication.

Revision ID: 005
Revises: 004
Create Date: 2026-04-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default="false"),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"])
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])

    # RLS — nullable tenant pattern (admin keys have tenant_id IS NULL)
    op.execute("ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_keys FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON api_keys
          USING (
            current_setting('app.current_tenant', true) = '__bypass__'
            OR current_setting('app.current_tenant', true) = '__all__'
            OR tenant_id = current_setting('app.current_tenant', true)
          )
          WITH CHECK (
            current_setting('app.current_tenant', true) = '__bypass__'
            OR current_setting('app.current_tenant', true) = '__all__'
            OR tenant_id = current_setting('app.current_tenant', true)
            OR tenant_id IS NULL
          )
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON api_keys")
    op.execute("ALTER TABLE api_keys DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_api_keys_tenant_id", "api_keys")
    op.drop_index("ix_api_keys_key_hash", "api_keys")
    op.drop_table("api_keys")
