"""Add Stars pricing snapshots and referred-user discount credit.

Revision ID: 0003_stars_referral
Revises: 0002_inventory_broadcast
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_stars_referral"
down_revision = "0002_inventory_broadcast"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    user_columns = _columns("users")
    if "referral_bonus_stars" not in user_columns:
        op.add_column("users", sa.Column("referral_bonus_stars", sa.BigInteger(), nullable=False, server_default="0"))

    order_columns = _columns("orders")
    additions = {
        "stars_subtotal": sa.Column("stars_subtotal", sa.BigInteger(), nullable=False, server_default="0"),
        "stars_promo_discount": sa.Column("stars_promo_discount", sa.BigInteger(), nullable=False, server_default="0"),
        "referral_bonus_stars_used": sa.Column("referral_bonus_stars_used", sa.BigInteger(), nullable=False, server_default="0"),
        "stars_bonus_restored": sa.Column("stars_bonus_restored", sa.Boolean(), nullable=False, server_default=sa.false()),
    }
    for name, column in additions.items():
        if name not in order_columns:
            op.add_column("orders", column)


def downgrade() -> None:
    order_columns = _columns("orders")
    for name in ("stars_bonus_restored", "referral_bonus_stars_used", "stars_promo_discount", "stars_subtotal"):
        if name in order_columns:
            op.drop_column("orders", name)
    user_columns = _columns("users")
    if "referral_bonus_stars" in user_columns:
        op.drop_column("users", "referral_bonus_stars")
