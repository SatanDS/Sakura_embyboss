"""Add the privacy-preserving Emby device observation registry."""

from alembic import op
import sqlalchemy as sa


revision = "20260929_14"
down_revision = "20260929_13"
branch_labels = None
depends_on = None


def upgrade():
    metadata = sa.MetaData()
    sa.Table(
        "emby_devices",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("emby_user_id", sa.String(255), nullable=False),
        sa.Column("tg", sa.BigInteger(), nullable=True),
        sa.Column("device_key_hash", sa.String(64), nullable=False),
        sa.Column("key_type", sa.String(16), nullable=False, server_default="device_id"),
        sa.Column("device_name", sa.String(255), nullable=True),
        sa.Column("client_name", sa.String(255), nullable=True),
        sa.Column("client_version", sa.String(128), nullable=True),
        sa.Column("first_seen", sa.DateTime(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        sa.Column("seen_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_ip_hash", sa.String(64), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("unbind_month", sa.String(7), nullable=True),
        sa.Column("unbind_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "emby_user_id", "device_key_hash", name="uq_emby_device_key"
        ),
        sa.Index("ix_emby_devices_user_id", "emby_user_id"),
        sa.Index("ix_emby_devices_tg", "tg"),
    )
    metadata.create_all(op.get_bind(), checkfirst=True)
    sa.Table(
        "emby_device_policies",
        metadata,
        sa.Column("tg", sa.BigInteger(), primary_key=True),
        sa.Column("device_limit", sa.Integer(), nullable=True),
        sa.Column("unbind_limit_per_month", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade():
    # Device observations are security/audit evidence.  Keep them on a
    # downgrade unless an operator explicitly removes the table.
    raise RuntimeError("Emby device observations must be retained")
