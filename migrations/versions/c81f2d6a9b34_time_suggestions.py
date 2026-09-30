"""time suggestions ("this time doesn't work for me")

Revision ID: c81f2d6a9b34
Revises: b3d9a1c47e21
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa

revision = 'c81f2d6a9b34'
down_revision = 'b3d9a1c47e21'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'time_suggestions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('client_account_id', sa.Integer(), sa.ForeignKey('client_accounts.id'), nullable=False, index=True),
        sa.Column('program', sa.String(length=40), nullable=False),
        sa.Column('times', sa.String(length=200), nullable=False),
        sa.Column('other', sa.String(length=200), nullable=True),
        sa.Column('name', sa.String(length=120), nullable=True),
        sa.Column('contact', sa.String(length=160), nullable=True),
        sa.Column('utm_source', sa.String(length=80), nullable=True),
        sa.Column('utm_campaign', sa.String(length=80), nullable=True),
        sa.Column('utm_content', sa.String(length=80), nullable=True),
        sa.Column('landing_variant', sa.String(length=60), nullable=True),
        sa.Column('submit_ip', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table('time_suggestions')
