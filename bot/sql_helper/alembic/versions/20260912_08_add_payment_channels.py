"""Keep mode-specific payment preferences and immutable Checkout snapshots."""

from alembic import op
import sqlalchemy as sa


revision = "20260912_08"
down_revision = "20260912_07"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("payment_orders")}
    if "payment_channels_snapshot" not in columns:
        op.add_column("payment_orders", sa.Column("payment_channels_snapshot", sa.JSON(), nullable=True))
    metadata = sa.MetaData()
    channels = sa.Table(
        "payment_channel_configs", metadata,
        sa.Column("mode", sa.String(8), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("channels", sa.JSON(), nullable=False),
        sa.Column("stripe_configuration_id", sa.String(255), nullable=True),
    )
    metadata.create_all(bind, checkfirst=True)
    # Old sessions retain a NULL snapshot so provider retries keep identical parameters.
    defaults = {"alipay": True, "wechat_pay": True, "card": False, "apple_pay": False, "google_pay": False}
    for mode in ("test", "live"):
        if bind.execute(sa.select(channels.c.mode).where(channels.c.mode == mode)).first() is None:
            bind.execute(channels.insert().values(mode=mode, version=1, channels=defaults))


def downgrade():
    raise RuntimeError("Keep payment channel snapshots; disable channels through payment administration")
