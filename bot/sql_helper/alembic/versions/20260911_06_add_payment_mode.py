"""Persist the Stripe test/live boundary on orders and codes."""

from alembic import op
import sqlalchemy as sa


revision = "20260911_06"
down_revision = "20260909_05"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in ("payment_orders", "payment_codes"):
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "mode" not in columns:
            op.add_column(table, sa.Column("mode", sa.String(length=8), nullable=False,
                                           server_default="live"))


def downgrade():
    raise RuntimeError("Payment mode history is irreversible; restore a database backup instead")
