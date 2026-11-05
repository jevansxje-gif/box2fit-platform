"""users: when they unsubscribed from marketing email

Revision ID: c6e4a1f7d392
Revises: b3f1c8d2a764
Create Date: 2026-11-05
"""
from alembic import op
import sqlalchemy as sa

revision = 'c6e4a1f7d392'
down_revision = 'b3f1c8d2a764'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('unsubscribed_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('users') as batch:
        batch.drop_column('unsubscribed_at')
