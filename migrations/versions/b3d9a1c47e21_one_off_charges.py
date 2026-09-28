"""one-off charges (punch cards etc.) paid via signed link

Revision ID: b3d9a1c47e21
Revises: 7d43bafb83a8
Create Date: 2026-09-28
"""
from alembic import op
import sqlalchemy as sa

revision = 'b3d9a1c47e21'
down_revision = '7d43bafb83a8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'one_off_charges',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('client_account_id', sa.Integer(), sa.ForeignKey('client_accounts.id'), nullable=False, index=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, index=True),
        sa.Column('description', sa.String(length=120), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('tax_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_cents', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='CAD'),
        sa.Column('status', sa.String(length=12), nullable=False, server_default='pending'),
        sa.Column('stripe_checkout_session_id', sa.String(length=128), nullable=True),
        sa.Column('stripe_payment_intent_id', sa.String(length=64), nullable=True),
        sa.Column('payment_id', sa.Integer(), sa.ForeignKey('payments.id'), nullable=True),
        sa.Column('created_by', sa.String(length=120), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('paid_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table('one_off_charges')
