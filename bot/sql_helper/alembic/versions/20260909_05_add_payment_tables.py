"""Add payment orders, codes, event outbox and account entitlement ledger."""

from alembic import op
import sqlalchemy as sa

revision = "20260909_05"
down_revision = "20260909_04"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    # Use metadata from the registered models so MySQL and SQLite receive the
    # same indexes/constraints without destructive alterations.
    from bot.payments.models import (Product, Order, Code, Event, Task, Audit,
                                     PaymentCapacity, RegistrationReservation,
                                     BrowserChallenge, BrowserSession)
    from bot.payments.entitlements import AccountEntitlement, AccountPeriod
    metadata = sa.MetaData()
    for model in (Product, Order, Code, Event, Task, Audit, PaymentCapacity,
                  RegistrationReservation, BrowserChallenge, BrowserSession,
                  AccountEntitlement, AccountPeriod):
        model.__table__.to_metadata(metadata)
    metadata.create_all(bind=bind, checkfirst=True)


def downgrade():
    # Payment history is operational evidence; do not delete it automatically.
    raise RuntimeError("Payment tables are irreversible; restore a database backup instead")
