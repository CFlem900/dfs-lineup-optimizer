"""add_nba_data_cache_tables

Revision ID: c7d8e9f0a1b2
Revises: b5c2d3e4f5a6
Create Date: 2026-02-16 12:00:00.000000

Creates ``nba_player_game_log`` and ``nba_team_stats`` tables for caching
NBA API data in PostgreSQL.  Background refresh jobs populate these tables
daily so the lineup optimizer reads from the DB instead of making ~140+
live API calls per slate.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, None] = 'b5c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── nba_player_game_log ──────────────────────────────────────
    op.create_table(
        'nba_player_game_log',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('player_id', sa.Integer(), nullable=False),
        sa.Column('player_name', sa.String(64), nullable=False),
        sa.Column('team_id', sa.Integer(), nullable=False),
        sa.Column('season', sa.String(7), nullable=False),
        sa.Column('game_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('matchup', sa.String(16), nullable=True),
        sa.Column('wl', sa.String(1), nullable=True),
        # Core box-score stats
        sa.Column('min', sa.Float(), nullable=True),
        sa.Column('pts', sa.Float(), nullable=True),
        sa.Column('reb', sa.Float(), nullable=True),
        sa.Column('ast', sa.Float(), nullable=True),
        sa.Column('stl', sa.Float(), nullable=True),
        sa.Column('blk', sa.Float(), nullable=True),
        sa.Column('tov', sa.Float(), nullable=True),
        sa.Column('fg3m', sa.Float(), nullable=True),
        # Shooting splits
        sa.Column('fgm', sa.Float(), nullable=True),
        sa.Column('fga', sa.Float(), nullable=True),
        sa.Column('fg_pct', sa.Float(), nullable=True),
        sa.Column('fg3a', sa.Float(), nullable=True),
        sa.Column('fg3_pct', sa.Float(), nullable=True),
        sa.Column('ftm', sa.Float(), nullable=True),
        sa.Column('fta', sa.Float(), nullable=True),
        sa.Column('ft_pct', sa.Float(), nullable=True),
        # Rebounds detail + misc
        sa.Column('oreb', sa.Float(), nullable=True),
        sa.Column('dreb', sa.Float(), nullable=True),
        sa.Column('pf', sa.Float(), nullable=True),
        sa.Column('plus_minus', sa.Float(), nullable=True),
        # IDs + metadata
        sa.Column('game_id', sa.String(16), nullable=True),
        sa.Column('raw_data', JSONB(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        # Constraints
        sa.UniqueConstraint('player_id', 'game_id', name='uq_npgl_player_game'),
    )
    op.create_index('ix_npgl_player_season', 'nba_player_game_log',
                     ['player_id', 'season'])
    op.create_index('ix_npgl_team_season', 'nba_player_game_log',
                     ['team_id', 'season'])
    op.create_index('ix_npgl_game_date', 'nba_player_game_log',
                     ['game_date'])

    # ── nba_team_stats ───────────────────────────────────────────
    op.create_table(
        'nba_team_stats',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('team_id', sa.Integer(), nullable=False),
        sa.Column('season', sa.String(7), nullable=False),
        sa.Column('stat_date', sa.DateTime(timezone=True), nullable=False),
        # Base per-game stats
        sa.Column('gp', sa.Integer(), nullable=True),
        sa.Column('ppg', sa.Float(), nullable=True),
        sa.Column('fgm', sa.Float(), nullable=True),
        sa.Column('fga', sa.Float(), nullable=True),
        sa.Column('ftm', sa.Float(), nullable=True),
        sa.Column('fta', sa.Float(), nullable=True),
        sa.Column('oreb', sa.Float(), nullable=True),
        sa.Column('dreb', sa.Float(), nullable=True),
        sa.Column('tov', sa.Float(), nullable=True),
        # Advanced
        sa.Column('pace', sa.Float(), nullable=True),
        sa.Column('off_rating', sa.Float(), nullable=True),
        sa.Column('def_rating', sa.Float(), nullable=True),
        sa.Column('w', sa.Integer(), nullable=True),
        sa.Column('l', sa.Integer(), nullable=True),
        # Opponent per-game stats (DvP)
        sa.Column('opp_pts_pg', sa.Float(), nullable=True),
        sa.Column('opp_reb_pg', sa.Float(), nullable=True),
        sa.Column('opp_ast_pg', sa.Float(), nullable=True),
        sa.Column('opp_stl_pg', sa.Float(), nullable=True),
        sa.Column('opp_blk_pg', sa.Float(), nullable=True),
        sa.Column('opp_tov_pg', sa.Float(), nullable=True),
        sa.Column('opp_fg3m_pg', sa.Float(), nullable=True),
        # JSONB blobs
        sa.Column('usage_rates', JSONB(), nullable=True),
        sa.Column('rosters', JSONB(), nullable=True),
        sa.Column('raw_base', JSONB(), nullable=True),
        sa.Column('raw_advanced', JSONB(), nullable=True),
        sa.Column('raw_opponent', JSONB(), nullable=True),
        # Metadata
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        # Constraints
        sa.UniqueConstraint('team_id', 'season', 'stat_date',
                            name='uq_nts_team_season_date'),
    )
    op.create_index('ix_nts_season_date', 'nba_team_stats',
                     ['season', 'stat_date'])
    op.create_index('ix_nts_team_date', 'nba_team_stats',
                     ['team_id', 'stat_date'])


def downgrade() -> None:
    op.drop_table('nba_team_stats')
    op.drop_table('nba_player_game_log')
