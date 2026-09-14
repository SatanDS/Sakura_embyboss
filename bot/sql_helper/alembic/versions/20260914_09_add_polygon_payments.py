"""Add Polygon USDT quotes, verified receipts and separate product prices."""
from alembic import op
import sqlalchemy as sa

revision = "20260914_09"
down_revision = "20260912_08"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    additions = {
        "payment_products": [sa.Column("usdt_price_units", sa.BigInteger(), nullable=False, server_default="0")],
        "payment_orders": [sa.Column("provider", sa.String(16), nullable=False, server_default="stripe"),
                           sa.Column("amount_usdt_units", sa.BigInteger(), nullable=True)],
    }
    for table, columns in additions.items():
        existing = {c["name"] for c in sa.inspect(bind).get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)
    if bind.dialect.name == "mysql":
        op.alter_column("payment_orders", "currency", existing_type=sa.String(3), type_=sa.String(8), existing_nullable=False)
    meta = sa.MetaData()
    config = sa.Table("payment_polygon_config", meta, sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
                     sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("version", sa.Integer(), nullable=False))
    sa.Table("payment_polygon_quotes", meta,
        sa.Column("id", sa.String(32), primary_key=True), sa.Column("buyer_tg", sa.BigInteger(), nullable=False, index=True),
        sa.Column("product_id", sa.String(32), nullable=False), sa.Column("product_snapshot", sa.JSON(), nullable=False),
        sa.Column("terms_version", sa.String(64), nullable=False), sa.Column("address", sa.String(42), nullable=False),
        sa.Column("network", sa.String(16), nullable=False), sa.Column("amount_units", sa.BigInteger(), nullable=False),
        sa.Column("start_block", sa.BigInteger(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False), sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("address", "amount_units", name="uq_polygon_quote_amount"))
    sa.Table("payment_polygon_cursors", meta, sa.Column("address", sa.String(42), primary_key=True),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
    sa.Table("payment_polygon_receipts", meta, sa.Column("order_id", sa.String(32), primary_key=True),
        sa.Column("tx_hash", sa.String(66), nullable=False), sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False), sa.Column("amount_units", sa.BigInteger(), nullable=False),
        sa.Column("deposit_id", sa.String(128), nullable=True, unique=True), sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("credited_at", sa.DateTime(), nullable=True), sa.UniqueConstraint("tx_hash", "log_index", name="uq_polygon_transfer"))
    meta.create_all(bind, checkfirst=True)
    if bind.execute(sa.select(config.c.id).where(config.c.id == 1)).first() is None:
        bind.execute(config.insert().values(id=1, enabled=False, version=1))


def downgrade():
    raise RuntimeError("Retain Polygon quotes and receipts; disable new sales through administration")
