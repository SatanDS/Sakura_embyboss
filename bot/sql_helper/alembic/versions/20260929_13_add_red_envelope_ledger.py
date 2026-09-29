"""Persist red-envelope escrow and per-user claims."""

from alembic import op
import sqlalchemy as sa


revision = "20260929_13"
down_revision = "20260915_10"
branch_labels = None
depends_on = None


def upgrade():
    metadata = sa.MetaData()
    sa.Table(
        "red_envelopes",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("sender_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_name", sa.String(255), nullable=False),
        sa.Column("total_amount", sa.Integer(), nullable=False),
        sa.Column("remaining_amount", sa.Integer(), nullable=False),
        sa.Column("total_members", sa.Integer(), nullable=False),
        sa.Column("remaining_members", sa.Integer(), nullable=False),
        sa.Column("envelope_type", sa.String(16), nullable=False),
        sa.Column("target_user", sa.BigInteger(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("refunded_amount", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Index("ix_red_envelopes_sender_id", "sender_id"),
        sa.Index("ix_red_envelopes_state", "state"),
    )
    sa.Table(
        "red_envelope_claims",
        metadata,
        sa.Column(
            "envelope_id",
            sa.String(64),
            sa.ForeignKey("red_envelopes.id"),
            primary_key=True,
        ),
        sa.Column("tg", sa.BigInteger(), primary_key=True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=False),
        sa.Index("ix_red_envelope_claims_tg", "tg"),
    )
    metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Red-envelope escrow and claim evidence must be retained")
