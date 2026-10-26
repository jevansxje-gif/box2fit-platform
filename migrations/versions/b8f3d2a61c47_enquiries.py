"""enquiries: call-first short form leads

Revision ID: b8f3d2a61c47
Revises: e5b2c7d1a903
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa

revision = 'b8f3d2a61c47'
down_revision = 'e5b2c7d1a903'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'enquiries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('client_account_id', sa.Integer(), sa.ForeignKey('client_accounts.id'), nullable=False),
        sa.Column('offer', sa.String(length=20), nullable=False),
        sa.Column('first_name', sa.String(length=60), nullable=False),
        sa.Column('last_name', sa.String(length=60), nullable=False),
        sa.Column('phone', sa.String(length=20), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('dob', sa.Date(), nullable=True),
        sa.Column('child_first_name', sa.String(length=60), nullable=True),
        sa.Column('child_dob', sa.Date(), nullable=True),
        sa.Column('best_time', sa.String(length=200), nullable=True),
        sa.Column('sms_ok', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='new'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('link_sent_at', sa.DateTime(), nullable=True),
        sa.Column('utm_source', sa.String(length=120), nullable=True),
        sa.Column('utm_medium', sa.String(length=120), nullable=True),
        sa.Column('utm_campaign', sa.String(length=255), nullable=True),
        sa.Column('utm_content', sa.String(length=255), nullable=True),
        sa.Column('landing_variant', sa.String(length=80), nullable=True),
        sa.Column('submit_ip', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_enquiries_client_account_id', 'enquiries', ['client_account_id'])
    op.create_index('ix_enquiries_offer', 'enquiries', ['offer'])
    op.create_index('ix_enquiries_phone', 'enquiries', ['phone'])
    op.create_index('ix_enquiries_status', 'enquiries', ['status'])
    op.create_index('ix_enquiries_user_id', 'enquiries', ['user_id'])


def downgrade():
    op.drop_table('enquiries')
