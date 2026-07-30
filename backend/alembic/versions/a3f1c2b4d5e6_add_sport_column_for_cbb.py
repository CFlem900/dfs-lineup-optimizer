"""add_sport_column_for_cbb

Revision ID: a3f1c2b4d5e6
Revises: d61e2f22188e
Create Date: 2026-02-15 12:00:00.000000

Adds a ``sport`` column (nba/cbb) to teams, player_minutes_history,
projection_log, and tournament_contest tables.  Also widens
teams.abbreviation from VARCHAR(3) to VARCHAR(10) for CBB abbreviations
and replaces the old unique constraint on abbreviation with a composite
(abbreviation, sport) constraint.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f1c2b4d5e6'
down_revision: Union[str, None] = 'd61e2f22188e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- teams table ---
    # Add sport column with default 'nba'
    op.add_column(
        'teams',
        sa.Column('sport', sa.String(3), nullable=False, server_default='nba'),
    )
    op.create_index('ix_teams_sport', 'teams', ['sport'])

    # Widen abbreviation from VARCHAR(3) to VARCHAR(10) for CBB
    op.alter_column(
        'teams',
        'abbreviation',
        type_=sa.String(10),
        existing_type=sa.String(3),
        existing_nullable=False,
    )

    # Drop old unique constraint on abbreviation (if exists) and add
    # composite unique constraint (abbreviation, sport).
    # The original schema may have an implicit unique or explicit one;
    # use batch mode for safety on various DB backends.
    try:
        op.drop_constraint('teams_abbreviation_key', 'teams', type_='unique')
    except Exception:
        pass  # constraint may not exist under this name

    op.create_unique_constraint(
        'uq_teams_abbr_sport', 'teams', ['abbreviation', 'sport']
    )

    # --- player_minutes_history table ---
    op.add_column(
        'player_minutes_history',
        sa.Column('sport', sa.String(3), nullable=False, server_default='nba'),
    )
    op.create_index('ix_pmh_sport', 'player_minutes_history', ['sport'])

    # --- projection_log table ---
    op.add_column(
        'projection_log',
        sa.Column('sport', sa.String(3), nullable=False, server_default='nba'),
    )
    op.create_index('ix_projection_log_sport', 'projection_log', ['sport'])

    # --- tournament_contest table ---
    op.add_column(
        'tournament_contest',
        sa.Column('sport', sa.String(3), nullable=False, server_default='nba'),
    )
    op.create_index('ix_tournament_contest_sport', 'tournament_contest', ['sport'])


def downgrade() -> None:
    # --- tournament_contest ---
    op.drop_index('ix_tournament_contest_sport', table_name='tournament_contest')
    op.drop_column('tournament_contest', 'sport')

    # --- projection_log ---
    op.drop_index('ix_projection_log_sport', table_name='projection_log')
    op.drop_column('projection_log', 'sport')

    # --- player_minutes_history ---
    op.drop_index('ix_pmh_sport', table_name='player_minutes_history')
    op.drop_column('player_minutes_history', 'sport')

    # --- teams ---
    op.drop_constraint('uq_teams_abbr_sport', 'teams', type_='unique')

    # Restore original abbreviation width
    op.alter_column(
        'teams',
        'abbreviation',
        type_=sa.String(3),
        existing_type=sa.String(10),
        existing_nullable=False,
    )

    # Restore simple unique constraint on abbreviation
    op.create_unique_constraint(
        'teams_abbreviation_key', 'teams', ['abbreviation']
    )

    op.drop_index('ix_teams_sport', table_name='teams')
    op.drop_column('teams', 'sport')
