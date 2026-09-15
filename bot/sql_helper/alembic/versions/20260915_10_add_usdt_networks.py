"""Add BSC and TON ledgers, preserving all Polygon and Stripe records."""
from alembic import op
import sqlalchemy as sa

revision = "20260915_10"
down_revision = "20260914_09"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    meta = sa.MetaData()
    configs = []
    for chain in ("bsc", "ton"):
        prefix = "payment_" + chain
        configs.append(sa.Table(prefix + "_config", meta,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("version", sa.Integer(), nullable=False)))
        sa.Table(prefix + "_quotes", meta,
            sa.Column("id", sa.String(32), primary_key=True), sa.Column("buyer_tg", sa.BigInteger(), nullable=False, index=True),
            sa.Column("product_id", sa.String(32), nullable=False), sa.Column("product_snapshot", sa.JSON(), nullable=False),
            sa.Column("terms_version", sa.String(64), nullable=False), sa.Column("address", sa.String(66), nullable=False),
            sa.Column("memo", sa.String(128), nullable=False), sa.Column("network", sa.String(16), nullable=False),
            sa.Column("amount_units", sa.BigInteger(), nullable=False), sa.Column("start_block", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("address", "amount_units", name="uq_" + chain + "_quote_amount"))
        sa.Table(prefix + "_cursors", meta, sa.Column("address", sa.String(66), primary_key=True),
            sa.Column("block_number", sa.BigInteger(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
        sa.Table(prefix + "_receipts", meta, sa.Column("order_id", sa.String(32), primary_key=True),
            sa.Column("tx_hash", sa.String(66), nullable=False), sa.Column("log_index", sa.Integer(), nullable=False),
            sa.Column("block_number", sa.BigInteger(), nullable=False), sa.Column("amount_units", sa.BigInteger(), nullable=False),
            sa.Column("deposit_id", sa.String(128), nullable=True, unique=True), sa.Column("received_at", sa.DateTime(), nullable=False),
            sa.Column("credited_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("tx_hash", "log_index", name="uq_" + chain + "_transfer"))
    claims = sa.Table("payment_chain_deposit_claims", meta,
        sa.Column("deposit_id", sa.String(128), primary_key=True),
        sa.Column("order_id", sa.String(32), nullable=False, unique=True))
    meta.create_all(bind, checkfirst=True)
    for config in configs:
        if bind.execute(sa.select(config.c.id).where(config.c.id == 1)).first() is None:
            bind.execute(config.insert().values(id=1, enabled=False, version=1))
    # Re-entry safe after interruption; never overwrite a conflicting claim.
    old = sa.Table("payment_polygon_receipts", sa.MetaData(), autoload_with=bind)
    for deposit_id, order_id in bind.execute(sa.select(old.c.deposit_id, old.c.order_id).where(old.c.deposit_id.isnot(None))):
        existing = bind.execute(sa.select(claims.c.order_id).where(claims.c.deposit_id == deposit_id)).scalar()
        if existing is None:
            bind.execute(claims.insert().values(deposit_id=deposit_id, order_id=order_id))
        elif existing != order_id:
            raise RuntimeError("Conflicting historical deposit claim")


def downgrade():
    raise RuntimeError("Disable network sales; retain all payment quotes and evidence")
