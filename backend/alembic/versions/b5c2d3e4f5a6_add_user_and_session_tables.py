"""add_user_and_session_tables

Revision ID: b5c2d3e4f5a6
Revises: a3f1c2b4d5e6
Create Date: 2026-02-15 18:00:00.000000

Creates ``users`` and ``user_sessions`` tables for OAuth authentication.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5c2d3e4f5a6'
down_revision: Union[str, None] = 'a3f1c2b4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('email', sa.String(256), nullable=False),
        sa.Column('display_name', sa.String(128), nullable=True),
        sa.Column('avatar_url', sa.String(512), nullable=True),
        sa.Column('oauth_provider', sa.String(16), nullable=False),
        sa.Column('oauth_provider_id', sa.String(128), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('email', name='uq_users_email'),
        sa.UniqueConstraint('oauth_provider', 'oauth_provider_id', name='uq_user_provider'),
    )
    op.create_index('ix_users_email', 'users', ['email'])

    op.create_table(
        'user_sessions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('session_token', sa.String(128), nullable=False),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('is_revoked', sa.Boolean(), server_default='false', nullable=False),
        sa.UniqueConstraint('session_token', name='uq_us_token'),
    )
    op.create_index('ix_us_token', 'user_sessions', ['session_token'])
    op.create_index('ix_us_user_expires', 'user_sessions', ['user_id', 'expires_at'])


def downgrade() -> None:
    op.drop_table('user_sessions')
    op.drop_table('users')
