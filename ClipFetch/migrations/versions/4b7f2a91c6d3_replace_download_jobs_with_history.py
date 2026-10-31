"""replace download jobs with filename history

Revision ID: 4b7f2a91c6d3
Revises: ec5d37667563
Create Date: 2026-10-31

"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = "4b7f2a91c6d3"
down_revision: Union[str, Sequence[str], None] = "ec5d37667563"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "download_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column(
            "downloaded_at",
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
    )
    op.create_index(
        "ix_download_history_user_downloaded_at",
        "download_history",
        ["user_id", "downloaded_at"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO download_history (user_id, filename, downloaded_at)
            SELECT user_id, filename, created_at
            FROM download_jobs
            WHERE status = 'completed'
              AND filename IS NOT NULL
              AND btrim(filename) <> ''
            """
        )
    )
    op.drop_table("download_jobs")


def downgrade() -> None:
    op.create_table(
        "download_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("video_url", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "media_type",
            sa.String(length=10),
            server_default="video",
            nullable=False,
        ),
        sa.Column(
            "quality",
            sa.String(length=20),
            server_default="best",
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    connection = op.get_bind()
    history = connection.execute(
        sa.text(
            """
            SELECT user_id, filename, downloaded_at
            FROM download_history
            ORDER BY id
            """
        )
    ).mappings().all()
    for record in history:
        connection.execute(
            sa.text(
                """
                INSERT INTO download_jobs
                    (id, user_id, video_url, status, created_at, media_type,
                     quality, filename)
                VALUES
                    (:id, :user_id, '', 'completed', :downloaded_at, 'video',
                     'best', :filename)
                """
            ),
            {
                "id": uuid.uuid4(),
                "user_id": record["user_id"],
                "downloaded_at": record["downloaded_at"],
                "filename": record["filename"],
            },
        )

    op.drop_index(
        "ix_download_history_user_downloaded_at",
        table_name="download_history",
    )
    op.drop_table("download_history")
