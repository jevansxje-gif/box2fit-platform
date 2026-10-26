"""enquiries: attendee, booked first class, card email sent

Revision ID: c4e1a9d7f203
Revises: b8f3d2a61c47
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa

revision = 'c4e1a9d7f203'
down_revision = 'b8f3d2a61c47'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('enquiries') as batch:
        batch.add_column(sa.Column('attendee_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_enquiries_attendee', 'attendee_profiles', ['attendee_id'], ['id'])
        batch.add_column(sa.Column('booked_for', sa.Date(), nullable=True))
        batch.add_column(sa.Column('card_sent_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('enquiries') as batch:
        batch.drop_column('card_sent_at')
        batch.drop_column('booked_for')
        batch.drop_constraint('fk_enquiries_attendee', type_='foreignkey')
        batch.drop_column('attendee_id')
