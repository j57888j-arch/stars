"""Remove unused card-proof payment storage; Stars is the only customer checkout.

Revision ID: 0004_stars_only
Revises: 0003_stars_referral
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_stars_only"
down_revision = "0003_stars_referral"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "payment_proofs" in tables:
        op.drop_table("payment_proofs")
    payment_columns = _columns("payments")
    removable = [name for name in ("proof_file_id", "reviewed_by", "reviewed_at") if name in payment_columns]
    if removable:
        with op.batch_alter_table("payments") as batch:
            for name in removable:
                batch.drop_column(name)


def downgrade() -> None:
    payment_columns = _columns("payments")
    with op.batch_alter_table("payments") as batch:
        if "proof_file_id" not in payment_columns:
            batch.add_column(sa.Column("proof_file_id", sa.String(length=256), nullable=True))
        if "reviewed_by" not in payment_columns:
            batch.add_column(sa.Column("reviewed_by", sa.BigInteger(), nullable=True))
        if "reviewed_at" not in payment_columns:
            batch.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))

    if "payment_proofs" not in _tables():
        op.create_table(
            "payment_proofs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("telegram_file_id", sa.String(length=256), nullable=False),
            sa.Column("uploaded_by", sa.BigInteger(), nullable=False),
            sa.Column("review_status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_payment_proofs_payment_id", "payment_proofs", ["payment_id"])
