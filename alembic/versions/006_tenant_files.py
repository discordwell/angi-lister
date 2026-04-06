"""Tenant files table for storing signature blocks, logos, and other uploads.

Revision ID: 006
Revises: 005
Create Date: 2026-04-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenant_files",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False, server_default="general"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_index("ix_tenant_files_tenant_id", "tenant_files", ["tenant_id"])

    # RLS — strict tenant (no nullable tenant_id)
    op.execute("ALTER TABLE tenant_files ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_files FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON tenant_files
          USING (
            current_setting('app.current_tenant', true) = '__bypass__'
            OR current_setting('app.current_tenant', true) = '__all__'
            OR tenant_id = current_setting('app.current_tenant', true)
          )
          WITH CHECK (
            current_setting('app.current_tenant', true) = '__bypass__'
            OR current_setting('app.current_tenant', true) = '__all__'
            OR tenant_id = current_setting('app.current_tenant', true)
          )
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tenant_files")
    op.execute("ALTER TABLE tenant_files DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_tenant_files_tenant_id", "tenant_files")
    op.drop_table("tenant_files")
