"""commission payouts: what the gym has already paid us

Revision ID: f2a8c1d4e6b9
Revises: c4e1a9d7f203
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'f2a8c1d4e6b9'
down_revision = 'c4e1a9d7f203'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'commission_payouts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('client_account_id', sa.Integer(), sa.ForeignKey('client_accounts.id'), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('paid_on', sa.Date(), nullable=False),
        sa.Column('note', sa.String(200), nullable=True),
        sa.Column('created_by', sa.String(120), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_commission_payouts_client_account_id', 'commission_payouts', ['client_account_id'])


def downgrade():
    op.drop_index('ix_commission_payouts_client_account_id', 'commission_payouts')
    op.drop_table('commission_payouts')
