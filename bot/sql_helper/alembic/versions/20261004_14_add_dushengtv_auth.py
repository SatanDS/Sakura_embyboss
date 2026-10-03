"""Persist independent DuShengTV login, devices, sessions and refresh rotation."""

from alembic import op
import sqlalchemy as sa

revision = "20261004_14"
down_revision = "20260929_13"
branch_labels = None
depends_on = None


def upgrade():
    metadata = sa.MetaData()
    sa.Table("tv_login_challenges", metadata,
             sa.Column("id", sa.String(32), primary_key=True),
             sa.Column("state_hash", sa.String(64), nullable=False),
             sa.Column("code_challenge", sa.String(43), nullable=False),
             sa.Column("link_hash", sa.String(64), nullable=False, unique=True),
             sa.Column("installation_id", sa.String(64), nullable=False),
             sa.Column("display_code", sa.String(8), nullable=False),
             sa.Column("status", sa.String(16), nullable=False),
             sa.Column("tg", sa.BigInteger()),
             sa.Column("display_name", sa.String(128)),
             sa.Column("username", sa.String(64)),
             sa.Column("expires_at", sa.DateTime(), nullable=False, index=True))
    sa.Table("tv_devices", metadata,
             sa.Column("id", sa.String(32), primary_key=True),
             sa.Column("tg", sa.BigInteger(), nullable=False, index=True),
             sa.Column("installation_id", sa.String(64), nullable=False),
             sa.Column("fingerprint", sa.String(64), nullable=False),
             sa.Column("public_key", sa.Text(), nullable=False),
             sa.Column("hardware", sa.JSON(), nullable=False),
             sa.Column("consent_version", sa.String(64), nullable=False),
             sa.Column("app_version", sa.String(32), nullable=False),
             sa.Column("created_at", sa.DateTime(), nullable=False),
             sa.Column("last_seen", sa.DateTime(), nullable=False),
             sa.Column("revoked_at", sa.DateTime()),
             sa.UniqueConstraint("tg", "installation_id", name="uq_tv_device_owner_install"))
    sa.Table("tv_sessions", metadata,
             sa.Column("id", sa.String(32), primary_key=True),
             sa.Column("tg", sa.BigInteger(), nullable=False, index=True),
             sa.Column("installation_id", sa.String(64), nullable=False),
             sa.Column("emby_user_id", sa.String(255), nullable=False),
             sa.Column("display_name", sa.String(128), nullable=False),
             sa.Column("username", sa.String(64), nullable=False),
             sa.Column("device_id", sa.String(32), index=True),
             sa.Column("access_hash", sa.String(64), nullable=False, unique=True),
             sa.Column("access_expires_at", sa.DateTime(), nullable=False),
             sa.Column("expires_at", sa.DateTime(), nullable=False, index=True),
             sa.Column("revoked_at", sa.DateTime()),
             sa.Column("nonce_hash", sa.String(64)),
             sa.Column("nonce_expires_at", sa.DateTime()))
    sa.Table("tv_refresh_tokens", metadata,
             sa.Column("token_hash", sa.String(64), primary_key=True),
             sa.Column("session_id", sa.String(32), nullable=False, index=True),
             sa.Column("used_at", sa.DateTime()),
             sa.Column("expires_at", sa.DateTime(), nullable=False, index=True))
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Desktop revocations and refresh replay evidence must be retained")
