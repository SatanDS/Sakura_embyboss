"""Record the DuShengTV login provider on durable sessions."""

from alembic import op
import sqlalchemy as sa

revision = "20261122_18"
down_revision = "20261101_17"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("tv_emby_owners"):
        op.create_table("tv_emby_owners",
                        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                                  primary_key=True, autoincrement=True),
                        sa.Column("emby_user_id", sa.String(255), nullable=False, unique=True))
    if "auth_provider" not in {column["name"] for column in inspector.get_columns("tv_sessions")}:
        op.add_column("tv_sessions", sa.Column("auth_provider", sa.String(16), nullable=False,
                                                 server_default="telegram"))


def downgrade():
    raise RuntimeError("Session provider history must be retained")
