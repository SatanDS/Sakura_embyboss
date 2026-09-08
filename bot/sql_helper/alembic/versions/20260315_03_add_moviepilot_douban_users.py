"""add Telegram to Douban bindings

Revision ID: 20260315_03
Revises: 20260315_02
Create Date: 2026-03-15 14:00:00
"""

from alembic import op


revision = "20260315_03"
down_revision = "20260315_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS `moviepilot_douban_users` (
          `tg` BIGINT NOT NULL,
          `douban_id` VARCHAR(20) NOT NULL,
          `created_at` DATETIME NOT NULL,
          `updated_at` DATETIME NOT NULL,
          PRIMARY KEY (`tg`),
          KEY `ix_moviepilot_douban_users_douban_id` (`douban_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `moviepilot_douban_users`;")
