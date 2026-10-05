"""One durable portable-preference backup per Telegram account."""

from alembic import op
import sqlalchemy as sa

revision = "20261005_15"
down_revision = "20261004_14"
branch_labels = None
depends_on = None


def upgrade():
    metadata = sa.MetaData()
    sa.Table("tv_cloud_settings", metadata,
             sa.Column("tg", sa.BigInteger(), primary_key=True, autoincrement=False),
             sa.Column("schema_version", sa.Integer(), nullable=False),
             sa.Column("revision", sa.Integer(), nullable=False),
             sa.Column("settings", sa.JSON(), nullable=False),
             sa.Column("updated_at", sa.DateTime(), nullable=False))
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Cloud preference backups must be retained")
