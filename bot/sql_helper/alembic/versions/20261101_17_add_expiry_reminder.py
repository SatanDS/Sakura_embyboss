"""Track the Emby account expiry successfully notified to Telegram.

Revision ID: 20261101_17
Revises: 20261006_16
"""

from alembic import op
import sqlalchemy as sa


revision = "20261101_17"
down_revision = "20261006_16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("emby")}
    if "expiry_reminded_for" not in columns:
        op.add_column("emby", sa.Column("expiry_reminded_for", sa.DateTime(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("emby")}
    if "expiry_reminded_for" in columns:
        op.drop_column("emby", "expiry_reminded_for")
