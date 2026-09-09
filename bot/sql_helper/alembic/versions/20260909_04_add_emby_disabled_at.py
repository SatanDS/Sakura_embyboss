"""Track the actual start of an Emby account's freeze period.

Revision ID: 20260909_04
Revises: 20260315_03
"""

from alembic import op
import sqlalchemy as sa


revision = "20260909_04"
down_revision = "20260315_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("emby")}
    if "disabled_at" not in columns:
        op.add_column("emby", sa.Column("disabled_at", sa.DateTime(), nullable=True))
    # Old disabled rows deliberately stay NULL: their freeze start is unknown.


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("emby")}
    if "disabled_at" in columns:
        op.drop_column("emby", "disabled_at")
