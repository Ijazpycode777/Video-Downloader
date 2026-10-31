"""add encrypted per-user provider cookies

Revision ID: 72c3a9d8e104
Revises: 4b7f2a91c6d3
Create Date: 2026-10-31

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "72c3a9d8e104"
down_revision: Union[str, Sequence[str], None] = "4b7f2a91c6d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_provider_cookies",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("encrypted_cookies", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "requires_reauthentication",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            name="uq_user_provider_cookies_user_provider",
        ),
    )
    op.create_index(
        op.f("ix_user_provider_cookies_user_id"),
        "user_provider_cookies",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_user_provider_cookies_user_id"),
        table_name="user_provider_cookies",
    )
    op.drop_table("user_provider_cookies")
