"""add_solver_tracking_tables

Revision ID: f82a3b19c7d0
Revises: d61e2f22188e
Create Date: 2026-02-26 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'f82a3b19c7d0'
down_revision: Union[str, None] = 'c7d8e9f0a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── solver_configurations ────────────────────────────────────
    op.create_table(
        'solver_configurations',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('config_hash', sa.String(64), unique=True, nullable=False),
        sa.Column('name', sa.String(128), nullable=True),
        sa.Column('optimality_threshold', sa.Float(), nullable=True),
        sa.Column('max_cumulative_ownership', sa.Float(), nullable=True),
        sa.Column('global_max_exposure', sa.Float(), nullable=True),
        sa.Column('salary_floor_pct', sa.Float(), nullable=True),
        sa.Column('max_overlap', sa.Integer(), nullable=True),
        sa.Column('strategy', sa.String(32), nullable=True),
        sa.Column('contest_type', sa.String(16), nullable=True),
        sa.Column('platform', sa.String(4), nullable=True),
        sa.Column('sport', sa.String(4), nullable=True),
        sa.Column('seed', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index('ix_sc_config_hash', 'solver_configurations', ['config_hash'], unique=True)
    op.create_index('ix_sc_strategy_contest', 'solver_configurations', ['strategy', 'contest_type'])

    # ── lineup_batches ───────────────────────────────────────────
    op.create_table(
        'lineup_batches',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('config_id', sa.Integer(),
                  sa.ForeignKey('solver_configurations.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('slate_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('draft_group_id', sa.Integer(), nullable=True),
        sa.Column('total_lineups_requested', sa.Integer(), nullable=False),
        sa.Column('total_lineups_generated', sa.Integer(), nullable=False),
        sa.Column('generation_time_ms', sa.Integer(), nullable=False),
        sa.Column('pool_size', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index('ix_lb_config_id', 'lineup_batches', ['config_id'])
    op.create_index('ix_lb_slate_date', 'lineup_batches', ['slate_date'])
    op.create_index('ix_lb_config_date', 'lineup_batches', ['config_id', 'slate_date'])

    # ── lineup_results ───────────────────────────────────────────
    op.create_table(
        'lineup_results',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('batch_id', sa.Integer(),
                  sa.ForeignKey('lineup_batches.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('lineup_hash', sa.String(64), nullable=False),
        sa.Column('projected_score', sa.Float(), nullable=False, server_default='0'),
        sa.Column('total_salary', sa.Integer(), nullable=True),
        sa.Column('quality_grade', sa.String(4), nullable=True),
        sa.Column('player_ids', JSONB(), nullable=True),
        sa.Column('actual_score', sa.Float(), nullable=True),
        sa.Column('entry_fee_total', sa.Float(), nullable=False, server_default='0'),
        sa.Column('winnings_total', sa.Float(), nullable=False, server_default='0'),
        sa.Column('net_profit', sa.Float(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index('ix_lr_batch_id', 'lineup_results', ['batch_id'])
    op.create_index('ix_lr_lineup_hash', 'lineup_results', ['lineup_hash'])
    op.create_index('ix_lr_batch_hash', 'lineup_results', ['batch_id', 'lineup_hash'])


def downgrade() -> None:
    op.drop_table('lineup_results')
    op.drop_table('lineup_batches')
    op.drop_table('solver_configurations')
