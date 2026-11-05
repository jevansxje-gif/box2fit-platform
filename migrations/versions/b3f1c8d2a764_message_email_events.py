"""messages: delivered, opened, clicked and failed times from the email provider

Revision ID: b3f1c8d2a764
Revises: a7d3e9b2c518
Create Date: 2026-11-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'b3f1c8d2a764'
down_revision = 'a7d3e9b2c518'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('messages') as batch:
        batch.add_column(sa.Column('delivered_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('opened_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('clicked_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('failed_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('messages') as batch:
        batch.drop_column('failed_at')
        batch.drop_column('clicked_at')
        batch.drop_column('opened_at')
        batch.drop_column('delivered_at')
