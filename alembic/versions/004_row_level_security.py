"""Add tenant_id to events/receipts/duplicates, enable PostgreSQL RLS on all tenant-owned tables.

Revision ID: 004
Revises: 003
Create Date: 2026-04-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables that get RLS with nullable tenant_id (NULL allowed in WITH CHECK)
NULLABLE_TENANT_TABLES = [
    "leads",
    "outbound_messages",
    "webhook_receipts",
    "lead_events",
    "magic_link_tokens",
    "console_sessions",
]

# Tables that get RLS with NOT NULL tenant_id (no NULL clause in WITH CHECK)
STRICT_TENANT_TABLES = [
    "duplicate_matches",
    "angi_account_mappings",
    "tenant_home_bases",
    "tenant_job_rules",
    "tenant_specials",
]

ALL_RLS_TABLES = NULLABLE_TENANT_TABLES + STRICT_TENANT_TABLES


def upgrade() -> None:
    # -- 1. Add tenant_id columns -------------------------------------------------
    op.add_column("webhook_receipts", sa.Column(
        "tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True,
    ))
    op.add_column("lead_events", sa.Column(
        "tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True,
    ))
    op.add_column("duplicate_matches", sa.Column(
        "tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True,
    ))

    # -- 2. Backfill tenant_id from parent records --------------------------------
    op.execute("""
        UPDATE webhook_receipts wr
        SET tenant_id = l.tenant_id
        FROM leads l
        WHERE l.receipt_id = wr.id AND l.tenant_id IS NOT NULL
    """)
    op.execute("""
        UPDATE lead_events le
        SET tenant_id = l.tenant_id
        FROM leads l
        WHERE le.lead_id = l.id AND l.tenant_id IS NOT NULL
    """)
    op.execute("""
        UPDATE duplicate_matches dm
        SET tenant_id = l.tenant_id
        FROM leads l
        WHERE dm.lead_id = l.id
    """)

    # -- 3. Make duplicate_matches.tenant_id NOT NULL after backfill ---------------
    op.alter_column("duplicate_matches", "tenant_id", nullable=False)

    # -- 4. Add indexes for RLS query performance ---------------------------------
    op.create_index("ix_webhook_receipts_tenant_id", "webhook_receipts", ["tenant_id"])
    op.create_index("ix_lead_events_tenant_id", "lead_events", ["tenant_id"])
    op.create_index("ix_duplicate_matches_tenant_id", "duplicate_matches", ["tenant_id"])
    # These may already exist from prior migrations; create only if missing
    for table in ["leads", "outbound_messages", "magic_link_tokens", "console_sessions"]:
        try:
            op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        except Exception:
            pass  # index already exists

    # -- 5. Enable RLS + create policies ------------------------------------------
    for table in ALL_RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

        allow_null = table in NULLABLE_TENANT_TABLES
        null_clause = "\n    OR tenant_id IS NULL" if allow_null else ""

        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (
                current_setting('app.current_tenant', true) = '__bypass__'
                OR current_setting('app.current_tenant', true) = '__all__'
                OR tenant_id = current_setting('app.current_tenant', true)
              )
              WITH CHECK (
                current_setting('app.current_tenant', true) = '__bypass__'
                OR current_setting('app.current_tenant', true) = '__all__'
                OR tenant_id = current_setting('app.current_tenant', true){null_clause}
              )
        """)


def downgrade() -> None:
    # Drop policies and disable RLS
    for table in ALL_RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # Drop indexes
    op.drop_index("ix_webhook_receipts_tenant_id", "webhook_receipts")
    op.drop_index("ix_lead_events_tenant_id", "lead_events")
    op.drop_index("ix_duplicate_matches_tenant_id", "duplicate_matches")

    # Drop columns
    op.drop_column("duplicate_matches", "tenant_id")
    op.drop_column("lead_events", "tenant_id")
    op.drop_column("webhook_receipts", "tenant_id")
