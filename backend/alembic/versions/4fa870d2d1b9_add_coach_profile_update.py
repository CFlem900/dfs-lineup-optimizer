"""add_coach_profile_update

Revision ID: 4fa870d2d1b9
Revises: 21a8a4848d07
Create Date: 2026-02-13 09:02:01.988588

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4fa870d2d1b9'
down_revision: Union[str, None] = '21a8a4848d07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Table may already exist if created outside of Alembic migrations
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'coach_profile_update' not in inspector.get_table_names():
        op.create_table(
            'coach_profile_update',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('team_id', sa.Integer(), nullable=False),
            sa.Column('coach_name', sa.String(length=64), nullable=False),
            sa.Column('starter_multiplier_adj', sa.Float(), nullable=True),
            sa.Column('bench_multiplier_adj', sa.Float(), nullable=True),
            sa.Column('star_multiplier_adj', sa.Float(), nullable=True),
            sa.Column('rotation_size_adj', sa.Integer(), nullable=True),
            sa.Column('b2b_penalty_adj', sa.Float(), nullable=True),
            sa.Column('pace_factor_adj', sa.Float(), nullable=True),
            sa.Column('confidence', sa.Float(), nullable=True),
            sa.Column('reasoning', sa.Text(), nullable=True),
            sa.Column('based_on_games', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('is_active', sa.Boolean(), server_default='true', nullable=True),
            sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_cpu_team', 'coach_profile_update', ['team_id'], unique=False)
        op.create_index(op.f('ix_coach_profile_update_team_id'), 'coach_profile_update', ['team_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_coach_profile_update_team_id'), table_name='coach_profile_update')
    op.drop_index('ix_cpu_team', table_name='coach_profile_update')
    op.drop_table('coach_profile_update')
