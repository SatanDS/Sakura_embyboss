"""Persist exact-media subscription deduplication and Telegram ownership."""
from alembic import op
import sqlalchemy as sa

revision = "20261006_16"
down_revision = "20261005_15"
branch_labels = None
depends_on = None


def upgrade():
    metadata = sa.MetaData()
    sa.Table("tv_media_requests", metadata,
             sa.Column("key", sa.String(100), primary_key=True),
             sa.Column("media_key", sa.String(80), nullable=False, index=True),
             sa.Column("season", sa.Integer()),
             sa.Column("item", sa.JSON(), nullable=False),
             sa.Column("state", sa.String(20), nullable=False),
             sa.Column("mp_id", sa.String(40)),
             sa.Column("error", sa.String(60)),
             sa.Column("created_at", sa.DateTime(), nullable=False),
             sa.Column("updated_at", sa.DateTime(), nullable=False))
    sa.Table("tv_media_request_owners", metadata,
             sa.Column("tg", sa.BigInteger(), primary_key=True, autoincrement=False),
             sa.Column("request_key", sa.String(100), primary_key=True),
             sa.Column("created_at", sa.DateTime(), nullable=False))
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Subscription ownership and uncertain-delivery records must be retained")
