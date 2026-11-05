"""users: manual overrides for the Members page sections

Revision ID: a7d3e9b2c518
Revises: f2a8c1d4e6b9
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7d3e9b2c518'
down_revision = 'f2a8c1d4e6b9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('keep_working_until', sa.Date(), nullable=True))
        batch.add_column(sa.Column('marked_lapsed_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('users') as batch:
        batch.drop_column('marked_lapsed_at')
        batch.drop_column('keep_working_until')
