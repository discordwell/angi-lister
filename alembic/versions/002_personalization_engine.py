"""Personalization engine — new tables and columns for LLM email generation.

Revision ID: 002
Revises: 001
Create Date: 2026-04-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- New columns on tenants -----------------------------------------------
    op.add_column("tenants", sa.Column("sample_email", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("llm_system_prompt", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("personalization_enabled", sa.Boolean(), server_default="false"))
    op.add_column("tenants", sa.Column("pricing_tiers", JSONB(astext_type=sa.Text()), nullable=True))

    # --- New columns on leads -------------------------------------------------
    op.add_column("leads", sa.Column("lat", sa.Float(), nullable=True))
    op.add_column("leads", sa.Column("lng", sa.Float(), nullable=True))
    op.add_column("leads", sa.Column("geocode_source", sa.String(), nullable=True))

    # --- New columns on outbound_messages -------------------------------------
    op.add_column("outbound_messages", sa.Column("personalization_context", JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("outbound_messages", sa.Column("llm_model", sa.String(), nullable=True))
    op.add_column("outbound_messages", sa.Column("llm_duration_ms", sa.Integer(), nullable=True))
    op.add_column("outbound_messages", sa.Column("generation_method", sa.String(), nullable=True))

    # --- tenant_home_bases ----------------------------------------------------
    op.create_table(
        "tenant_home_bases",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("address", sa.String(), nullable=True),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_index("ix_tenant_home_bases_tenant_id", "tenant_home_bases", ["tenant_id"])

    # --- tenant_job_rules -----------------------------------------------------
    op.create_table(
        "tenant_job_rules",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("category_pattern", sa.String(), nullable=False),
        sa.Column("rule_type", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_index("ix_tenant_job_rules_tenant_id", "tenant_job_rules", ["tenant_id"])

    # --- tenant_specials ------------------------------------------------------
    op.create_table(
        "tenant_specials",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("discount_text", sa.String(), nullable=False),
        sa.Column("conditions", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_index("ix_tenant_specials_tenant_id", "tenant_specials", ["tenant_id"])

    # --- geocode_cache --------------------------------------------------------
    op.create_table(
        "geocode_cache",
        sa.Column("postal_code", sa.String(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("full_address", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("postal_code"),
    )

    # --- Indexes for repeat customer query ------------------------------------
    op.create_index("ix_leads_tenant_email", "leads", ["tenant_id", "email"])
    op.create_index("ix_leads_tenant_phone", "leads", ["tenant_id", "phone"])


def downgrade() -> None:
    op.drop_index("ix_leads_tenant_phone", table_name="leads")
    op.drop_index("ix_leads_tenant_email", table_name="leads")
    op.drop_table("geocode_cache")
    op.drop_table("tenant_specials")
    op.drop_table("tenant_job_rules")
    op.drop_table("tenant_home_bases")
    op.drop_column("outbound_messages", "generation_method")
    op.drop_column("outbound_messages", "llm_duration_ms")
    op.drop_column("outbound_messages", "llm_model")
    op.drop_column("outbound_messages", "personalization_context")
    op.drop_column("leads", "geocode_source")
    op.drop_column("leads", "lng")
    op.drop_column("leads", "lat")
    op.drop_column("tenants", "pricing_tiers")
    op.drop_column("tenants", "personalization_enabled")
    op.drop_column("tenants", "llm_system_prompt")
    op.drop_column("tenants", "sample_email")
