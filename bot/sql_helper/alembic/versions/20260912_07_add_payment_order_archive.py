"""Archive payment orders without deleting payment or entitlement history."""

from alembic import op
import sqlalchemy as sa


revision = "20260912_07"
down_revision = "20260911_06"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("payment_orders")}
    if "archived_at" not in columns:
        op.add_column("payment_orders", sa.Column("archived_at", sa.DateTime(), nullable=True))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("payment_orders")}
    if "ix_payment_orders_archived_at" not in indexes:
        op.create_index("ix_payment_orders_archived_at", "payment_orders", ["archived_at"])


def downgrade():
    raise RuntimeError("Keep payment archive history; restore order visibility through the archive API")
