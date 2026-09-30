"""subscription.challenge_key (paid cohort challenge, then membership)

Revision ID: d47a90b1c5e2
Revises: c81f2d6a9b34
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa

revision = 'd47a90b1c5e2'
down_revision = 'c81f2d6a9b34'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('subscriptions') as batch:
        batch.add_column(sa.Column('challenge_key', sa.String(length=40), nullable=True))
        batch.create_index('ix_subscriptions_challenge_key', ['challenge_key'])


def downgrade():
    with op.batch_alter_table('subscriptions') as batch:
        batch.drop_index('ix_subscriptions_challenge_key')
        batch.drop_column('challenge_key')
