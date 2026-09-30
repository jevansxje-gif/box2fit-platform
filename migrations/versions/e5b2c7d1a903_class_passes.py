"""class passes: attendee.pass_until/pass_segment; one_off_charges grant them

Revision ID: e5b2c7d1a903
Revises: d47a90b1c5e2
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5b2c7d1a903'
down_revision = 'd47a90b1c5e2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('attendee_profiles') as batch:
        batch.add_column(sa.Column('pass_until', sa.Date(), nullable=True))
        batch.add_column(sa.Column('pass_segment', sa.String(length=40), nullable=True))
    with op.batch_alter_table('one_off_charges') as batch:
        batch.add_column(sa.Column('attendee_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_one_off_charges_attendee', 'attendee_profiles', ['attendee_id'], ['id'])
        batch.add_column(sa.Column('pass_days', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('pass_segment', sa.String(length=40), nullable=True))


def downgrade():
    with op.batch_alter_table('one_off_charges') as batch:
        batch.drop_constraint('fk_one_off_charges_attendee', type_='foreignkey')
        batch.drop_column('pass_segment')
        batch.drop_column('pass_days')
        batch.drop_column('attendee_id')
    with op.batch_alter_table('attendee_profiles') as batch:
        batch.drop_column('pass_segment')
        batch.drop_column('pass_until')
