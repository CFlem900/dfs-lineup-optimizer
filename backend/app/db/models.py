"""SQLAlchemy ORM models for RotationEngine historical data.

Tables:
    users               — Authenticated users from OAuth login
    user_sessions       — Server-side sessions tied to HTTP-only cookies
    teams               — NBA + CBB teams (reference table, sport-partitioned)
    player_minutes_history — Actual vs projected minutes per player per game
    projection_log      — Full TeamRotation snapshots per game
    expert_signal_log   — Archived expert signals for historical analysis
    tournament_contest  — Imported DraftKings contest metadata
    tournament_entry    — Individual lineup entries from contests
    tournament_calibration — Learned adjustments from tournament analysis
    coach_profile_update — Learned coaching pattern adjustments from AI analysis
    nba_player_game_log — Cached NBA player game logs from stats.nba.com
    nba_team_stats      — Cached NBA team stats (base + advanced + opponent + rosters + usage)
    nba_injuries        — Synced NBA injury report from official NBA.com PDF
    solver_configurations — Distinct solver setting snapshots (deduplicated by param hash)
    lineup_batches      — Per-run metadata: slate date, timing, counts
    lineup_results      — Per-lineup projected / actual scores + P&L
    active_entries      — Imported DraftKings contest entries for late-swap
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


class User(Base):
    """Authenticated user from OAuth login."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(256), nullable=False, unique=True, index=True)
    display_name = Column(String(128), nullable=True)
    avatar_url = Column(String(512), nullable=True)
    oauth_provider = Column(String(16), nullable=False)
    oauth_provider_id = Column(String(128), nullable=False)
    is_active = Column(Boolean, default=True, server_default="true")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    sessions = relationship("UserSession", back_populates="user", lazy="dynamic")

    __table_args__ = (
        UniqueConstraint("oauth_provider", "oauth_provider_id", name="uq_user_provider"),
    )

    def __repr__(self):
        return f"<User {self.email} via {self.oauth_provider}>"


class UserSession(Base):
    """Server-side session tied to an HTTP-only cookie."""

    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_token = Column(String(128), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_revoked = Column(Boolean, default=False, server_default="false")

    user = relationship("User", back_populates="sessions")

    __table_args__ = (
        Index("ix_us_token", "session_token"),
        Index("ix_us_user_expires", "user_id", "expires_at"),
    )

    def __repr__(self):
        return f"<UserSession user={self.user_id} revoked={self.is_revoked}>"


class TeamRecord(Base):
    """NBA / CBB team reference table (sport-partitioned)."""

    __tablename__ = "teams"

    id = Column(Integer, primary_key=True)  # nba_api or ESPN team_id
    full_name = Column(String(64), nullable=False)
    abbreviation = Column(String(10), nullable=False)  # CBB abbrs can be >3 chars
    city = Column(String(32))
    nickname = Column(String(32))
    sport = Column(String(3), nullable=False, default="nba", index=True)

    # Relationships
    minutes_history = relationship(
        "PlayerMinutesHistory", back_populates="team", lazy="dynamic"
    )
    projection_logs = relationship(
        "ProjectionLog", back_populates="team", lazy="dynamic"
    )

    __table_args__ = (
        UniqueConstraint("abbreviation", "sport", name="uq_teams_abbr_sport"),
    )

    def __repr__(self):
        return f"<Team {self.abbreviation} ({self.full_name}) sport={self.sport}>"


class PlayerMinutesHistory(Base):
    """Historical record of projected vs actual player minutes.

    After each game date, a background job stores actual minutes from
    the box score alongside the projection that was generated before
    tip-off.  This powers the /api/accuracy endpoint for backtesting.
    """

    __tablename__ = "player_minutes_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, nullable=False, index=True)
    player_name = Column(String(64), nullable=False)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    game_date = Column(DateTime(timezone=True), nullable=False, index=True)
    actual_minutes = Column(Float, nullable=False)
    projected_minutes = Column(Float, nullable=True)  # our prediction
    baseline_minutes = Column(Float, nullable=True)  # pre-adjustment baseline
    position = Column(String(4), index=True)
    was_injured = Column(Integer, default=0)  # 0=available, 1=out
    dk_salary = Column(Integer, nullable=True)  # DK salary if available
    dk_projected_fp = Column(Float, nullable=True)
    dk_actual_fp = Column(Float, nullable=True)
    sport = Column(String(3), nullable=False, default="nba", index=True)

    # Per-stat actuals (for projection accuracy analysis by stat category)
    actual_pts = Column(Float, nullable=True)
    actual_reb = Column(Float, nullable=True)
    actual_ast = Column(Float, nullable=True)
    actual_stl = Column(Float, nullable=True)
    actual_blk = Column(Float, nullable=True)
    actual_tov = Column(Float, nullable=True)
    actual_fg3m = Column(Float, nullable=True)

    # Per-stat projections (stored at ingestion time for comparison)
    projected_pts = Column(Float, nullable=True)
    projected_reb = Column(Float, nullable=True)
    projected_ast = Column(Float, nullable=True)
    projected_stl = Column(Float, nullable=True)
    projected_blk = Column(Float, nullable=True)
    projected_tov = Column(Float, nullable=True)
    projected_fg3m = Column(Float, nullable=True)

    # Shooting profile actuals (for shot-type decomposition)
    actual_fg3a_rate = Column(Float, nullable=True)   # 3PA per minute
    actual_fga_rate = Column(Float, nullable=True)    # FGA per minute
    actual_fta_rate = Column(Float, nullable=True)    # FTA per minute
    actual_fg3_pct = Column(Float, nullable=True)     # 3P%
    actual_fg2_pct = Column(Float, nullable=True)     # 2P%
    actual_ft_pct = Column(Float, nullable=True)      # FT%

    # Opponent identification (for DvP grouping in calibration)
    opponent_team_id = Column(Integer, nullable=True, index=True)

    # Projected shooting rates (stored at ingestion for comparison)
    projected_fg3a_rate = Column(Float, nullable=True)  # Projected 3PA per minute
    projected_fga_rate = Column(Float, nullable=True)   # Projected FGA per minute
    projected_fta_rate = Column(Float, nullable=True)   # Projected FTA per minute

    # Relationships
    team = relationship("TeamRecord", back_populates="minutes_history")

    __table_args__ = (
        Index("ix_pmh_player_date", "player_id", "game_date"),
        Index("ix_pmh_team_date", "team_id", "game_date"),
        UniqueConstraint("player_id", "game_date", name="uq_pmh_player_game_date"),
    )

    def __repr__(self):
        return (
            f"<PlayerMinutes {self.player_name} "
            f"date={self.game_date} actual={self.actual_minutes}>"
        )


class ProjectionLog(Base):
    """Snapshot of a full TeamRotation projection for a game date.

    Stores the entire projection payload (as JSON) along with metadata
    about what injuries and coach profile were active when the
    projection was generated.
    """

    __tablename__ = "projection_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    game_date = Column(DateTime(timezone=True), nullable=False)
    projection_data = Column(JSONB, nullable=False)  # full TeamRotation as dict
    injuries_at_time = Column(JSONB, nullable=True)  # injuries when projection ran
    coach_profile_used = Column(String(64), nullable=True)
    total_minutes = Column(Float, nullable=True)
    player_count = Column(Integer, nullable=True)
    sport = Column(String(3), nullable=False, default="nba", index=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    team = relationship("TeamRecord", back_populates="projection_logs")

    __table_args__ = (
        Index("ix_pl_team_date", "team_id", "game_date"),
    )

    def __repr__(self):
        return f"<ProjectionLog team={self.team_id} date={self.game_date}>"


class ExpertSignalLog(Base):
    """Archived expert signal for historical trend analysis.

    Signals are deduplicated by signal_id (the same hash used by the
    live ExpertSignalService).
    """

    __tablename__ = "expert_signal_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(String(16), unique=True, nullable=False, index=True)
    expert_handle = Column(String(64), nullable=False, index=True)
    expert_name = Column(String(64), nullable=True)
    expert_tier = Column(String(10), nullable=True)
    content = Column(Text, nullable=False)
    signal_type = Column(String(24), nullable=True)
    sentiment = Column(String(10), nullable=True)
    relevance_score = Column(Float, nullable=True)
    mentioned_players = Column(JSONB, nullable=True)
    mentioned_team = Column(String(10), nullable=True)
    source_platform = Column(String(10), nullable=True)
    source_url = Column(String(512), nullable=True)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_esl_handle_ts", "expert_handle", "timestamp"),
    )

    def __repr__(self):
        return f"<ExpertSignalLog @{self.expert_handle} id={self.signal_id}>"


class ExpertQualityScore(Base):
    """Tracks historical accuracy of expert signal sources.

    Populated by Agent 10 (ExpertQualityAgent) to adjust expert
    weighting based on prediction accuracy.
    """

    __tablename__ = "expert_quality_score"

    id = Column(Integer, primary_key=True, autoincrement=True)
    expert_handle = Column(String(64), nullable=False, index=True)
    evaluation_date = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    accuracy_score = Column(Float, nullable=False)  # 0.0–1.0
    total_predictions = Column(Integer, default=0)
    correct_predictions = Column(Integer, default=0)
    specialties = Column(JSONB, nullable=True)  # {signal_type: accuracy}
    tier_recommendation = Column(String(10), nullable=True)
    weight_modifier = Column(Float, default=1.0)  # 0.5–1.5

    __table_args__ = (
        Index("ix_eqs_handle_date", "expert_handle", "evaluation_date"),
    )

    def __repr__(self):
        return (
            f"<ExpertQualityScore @{self.expert_handle} "
            f"accuracy={self.accuracy_score:.2f}>"
        )


class BacktestCalibration(Base):
    """Stores calibration adjustments derived from backtesting analysis.

    Populated by Agent 9 (BacktestingAgent) after analysing historical
    projection accuracy.  Used to adjust future projections.
    """

    __tablename__ = "backtest_calibration"

    id = Column(Integer, primary_key=True, autoincrement=True)
    calibration_key = Column(String(64), nullable=False, unique=True)
    adjustment_value = Column(Float, nullable=False)  # multiplier, e.g. 0.95
    based_on_games = Column(Integer, default=0)
    analysis_date = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    reasoning = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True, server_default="true")

    __table_args__ = (
        Index("ix_bc_key", "calibration_key"),
        Index("ix_bc_active_expiry", "is_active", "expires_at"),
    )

    def __repr__(self):
        return (
            f"<BacktestCalibration {self.calibration_key}="
            f"{self.adjustment_value:.3f}>"
        )


class TournamentContest(Base):
    """Metadata for an imported DraftKings contest.

    Each CSV upload creates one contest record.  The ``source`` column
    distinguishes CSV imports from future DK scraping.
    """

    __tablename__ = "tournament_contest"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contest_id = Column(String(32), nullable=True, unique=True, index=True)
    contest_name = Column(String(256), nullable=True)
    contest_date = Column(DateTime(timezone=True), nullable=False, index=True)
    platform = Column(String(8), default="dk")  # "dk" or "fd"
    sport = Column(String(3), nullable=False, default="nba", index=True)
    entry_count = Column(Integer, nullable=True)
    prize_pool = Column(Float, nullable=True)
    contest_type = Column(String(16), nullable=True)  # gpp, cash, single_entry
    source = Column(String(16), default="csv")  # "csv" or "scrape"
    import_metadata = Column(JSONB, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    entries = relationship(
        "TournamentEntry", back_populates="contest", lazy="dynamic"
    )

    __table_args__ = (
        Index("ix_tc_date_platform", "contest_date", "platform"),
    )

    def __repr__(self):
        return f"<TournamentContest {self.contest_name} date={self.contest_date}>"


class TournamentEntry(Base):
    """A single lineup entry from a DraftKings contest export.

    The ``lineup_data`` JSONB column stores the full player list as
    structured JSON (list of dicts with roster_slot, player_name, salary).
    The ``is_winner`` flag marks top-1% entries for agent analysis.
    """

    __tablename__ = "tournament_entry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contest_id_fk = Column(
        Integer, ForeignKey("tournament_contest.id"), nullable=False
    )
    rank = Column(Integer, nullable=False)
    entry_id = Column(String(32), nullable=True)
    entry_name = Column(String(128), nullable=True)
    points = Column(Float, nullable=False)
    lineup_data = Column(JSONB, nullable=False)
    total_salary = Column(Integer, nullable=True)
    is_winner = Column(Integer, default=0)  # 1 = top 1%

    contest = relationship("TournamentContest", back_populates="entries")

    __table_args__ = (
        Index("ix_te_contest_rank", "contest_id_fk", "rank"),
        Index("ix_te_points", "points"),
        Index("ix_te_contest_winner", "contest_id_fk", "is_winner"),
    )

    def __repr__(self):
        return f"<TournamentEntry rank={self.rank} pts={self.points}>"


class TournamentCalibration(Base):
    """Learned calibration adjustments from tournament result analysis.

    Populated by Agent 12 (TournamentAnalysisAgent).  Parallels
    BacktestCalibration but adds ``category``, ``confidence``, and
    ``raw_adjustment`` (the uncapped value from the agent) for
    transparency.  Categories: position_bias, salary_tier,
    stacking_weight, ownership_threshold, game_context.
    """

    __tablename__ = "tournament_calibration"

    id = Column(Integer, primary_key=True, autoincrement=True)
    calibration_key = Column(String(64), nullable=False, unique=True)
    category = Column(String(32), nullable=False, index=True)
    adjustment_value = Column(Float, nullable=False)  # capped multiplier
    raw_adjustment = Column(Float, nullable=True)  # uncapped from agent
    based_on_contests = Column(Integer, default=0)
    based_on_entries = Column(Integer, default=0)
    confidence = Column(Float, default=0.5)
    analysis_date = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    reasoning = Column(Text, nullable=True)
    source = Column(String(16), default="tournament")
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True, server_default="true")

    __table_args__ = (
        Index("ix_tcal_category", "category"),
        Index("ix_tcal_key", "calibration_key"),
        Index("ix_tcal_active_expiry", "is_active", "expires_at"),
    )

    def __repr__(self):
        return (
            f"<TournamentCalibration {self.calibration_key}="
            f"{self.adjustment_value:.3f}>"
        )


class CoachProfileUpdate(Base):
    """Stores learned coaching pattern adjustments from AI analysis.

    Populated by Agent 6 (CoachLearningAgent).  These deltas are merged
    with the static COACH_PROFILES on startup and when projecting rotations.
    """

    __tablename__ = "coach_profile_update"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False, index=True)
    coach_name = Column(String(64), nullable=False)
    starter_multiplier_adj = Column(Float, default=0.0)
    bench_multiplier_adj = Column(Float, default=0.0)
    star_multiplier_adj = Column(Float, default=0.0)
    rotation_size_adj = Column(Integer, default=0)
    b2b_penalty_adj = Column(Float, default=0.0)
    pace_factor_adj = Column(Float, default=0.0)
    confidence = Column(Float, default=0.5)
    reasoning = Column(Text, nullable=True)
    based_on_games = Column(Integer, default=0)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True, server_default="true")

    __table_args__ = (
        Index("ix_cpu_team", "team_id"),
    )


class PlayerPropSnapshot(Base):
    """Historical snapshot of DK Sportsbook prop lines.

    Captured daily for each player with available props.  After game
    completion, ``actual_value`` is backfilled so we can measure
    projection accuracy vs. market accuracy over time.
    """

    __tablename__ = "player_prop_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_name = Column(String(64), nullable=False)
    team_abbreviation = Column(String(10), nullable=False)
    game_date = Column(DateTime(timezone=True), nullable=False)
    stat = Column(String(8), nullable=False)       # "pts", "reb", "ast", etc.
    line = Column(Float, nullable=False)            # DK prop O/U number
    over_odds = Column(Integer, nullable=True)      # American odds
    under_odds = Column(Integer, nullable=True)     # American odds
    implied_over_prob = Column(Float, nullable=True)
    our_projection = Column(Float, nullable=True)   # Our projected value at snapshot time
    actual_value = Column(Float, nullable=True)     # Backfilled after game
    captured_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_pps_player_date", "player_name", "game_date"),
        Index("ix_pps_date_stat", "game_date", "stat"),
        UniqueConstraint(
            "player_name", "game_date", "stat",
            name="uq_pps_player_date_stat",
        ),
    )

    def __repr__(self):
        return (
            f"<PlayerPropSnapshot {self.player_name} "
            f"{self.stat}={self.line} date={self.game_date}>"
        )


class AIUsageLog(Base):
    """Tracks LLM API usage per agent call for cost monitoring."""

    __tablename__ = "ai_usage_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_name = Column(String(64), nullable=False, index=True)
    model_used = Column(String(64), nullable=False)
    provider = Column(String(16), nullable=False)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    estimated_cost_usd = Column(Float, nullable=True)
    latency_ms = Column(Integer, default=0)
    cached = Column(Boolean, default=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_ai_usage_agent", "agent_name"),
        Index("ix_ai_usage_created", "created_at"),
        Index("ix_ai_usage_agent_created", "agent_name", "created_at"),
    )


class NBAPlayerGameLog(Base):
    """Cached NBA player game log from stats.nba.com PlayerGameLog endpoint.

    Populated by the daily background cache refresh job.  The lineup
    optimizer reads from this table instead of making ~140 live API calls
    per slate, reducing pool build time from 5-10 min to <5 seconds.
    """

    __tablename__ = "nba_player_game_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, nullable=False)
    player_name = Column(String(64), nullable=False)
    team_id = Column(Integer, nullable=False)
    season = Column(String(7), nullable=False)  # e.g. "2025-26"
    game_date = Column(DateTime(timezone=True), nullable=False)
    matchup = Column(String(16), nullable=True)  # e.g. "LAL vs. BOS"
    wl = Column(String(1), nullable=True)  # W or L

    # Core box-score stats
    min = Column(Float, nullable=True)
    pts = Column(Float, nullable=True)
    reb = Column(Float, nullable=True)
    ast = Column(Float, nullable=True)
    stl = Column(Float, nullable=True)
    blk = Column(Float, nullable=True)
    tov = Column(Float, nullable=True)
    fg3m = Column(Float, nullable=True)

    # Shooting splits
    fgm = Column(Float, nullable=True)
    fga = Column(Float, nullable=True)
    fg_pct = Column(Float, nullable=True)
    fg3a = Column(Float, nullable=True)
    fg3_pct = Column(Float, nullable=True)
    ftm = Column(Float, nullable=True)
    fta = Column(Float, nullable=True)
    ft_pct = Column(Float, nullable=True)

    # Rebounds detail + misc
    oreb = Column(Float, nullable=True)
    dreb = Column(Float, nullable=True)
    pf = Column(Float, nullable=True)
    plus_minus = Column(Float, nullable=True)

    game_id = Column(String(16), nullable=True)  # NBA game ID
    raw_data = Column(JSONB, nullable=True)  # Full original API row
    fetched_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("player_id", "game_id", name="uq_npgl_player_game"),
        Index("ix_npgl_player_season", "player_id", "season"),
        Index("ix_npgl_team_season", "team_id", "season"),
        Index("ix_npgl_game_date", "game_date"),
    )

    def __repr__(self):
        return (
            f"<NBAPlayerGameLog {self.player_name} "
            f"date={self.game_date} pts={self.pts}>"
        )


class NBATeamStats(Base):
    """Cached NBA team-level stats from LeagueDashTeamStats + rosters + usage.

    Stores the merged result of three NBA API calls (Base, Advanced,
    Opponent) plus CommonTeamRoster and LeagueDashPlayerBioStats.
    Refreshed daily by the background cache job.
    """

    __tablename__ = "nba_team_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, nullable=False)
    season = Column(String(7), nullable=False)
    stat_date = Column(DateTime(timezone=True), nullable=False)

    # Base per-game stats
    gp = Column(Integer, nullable=True)
    ppg = Column(Float, nullable=True)
    fgm = Column(Float, nullable=True)
    fga = Column(Float, nullable=True)
    ftm = Column(Float, nullable=True)
    fta = Column(Float, nullable=True)
    oreb = Column(Float, nullable=True)
    dreb = Column(Float, nullable=True)
    tov = Column(Float, nullable=True)

    # Advanced
    pace = Column(Float, nullable=True)
    off_rating = Column(Float, nullable=True)
    def_rating = Column(Float, nullable=True)
    w = Column(Integer, nullable=True)
    l = Column(Integer, nullable=True)

    # Opponent per-game stats (for DvP)
    opp_pts_pg = Column(Float, nullable=True)
    opp_reb_pg = Column(Float, nullable=True)
    opp_ast_pg = Column(Float, nullable=True)
    opp_stl_pg = Column(Float, nullable=True)
    opp_blk_pg = Column(Float, nullable=True)
    opp_tov_pg = Column(Float, nullable=True)
    opp_fg3m_pg = Column(Float, nullable=True)

    # JSONB blobs for roster + usage + raw API data
    usage_rates = Column(JSONB, nullable=True)  # {player_id_str: usg_pct}
    rosters = Column(JSONB, nullable=True)  # [{player_id, player_name, position, birth_date}, ...]
    raw_base = Column(JSONB, nullable=True)
    raw_advanced = Column(JSONB, nullable=True)
    raw_opponent = Column(JSONB, nullable=True)

    fetched_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("team_id", "season", "stat_date", name="uq_nts_team_season_date"),
        Index("ix_nts_season_date", "season", "stat_date"),
        Index("ix_nts_team_date", "team_id", "stat_date"),
    )


class NBAInjury(Base):
    """Persisted NBA injury report synced from the official NBA.com PDF.

    Populated by ``InjurySyncService.run_sync()`` and
    ``InjuryService.sync_injuries()`` using the ``nbainjuries`` library.
    Serves as the source of truth for the downstream Monte Carlo
    simulator that redistributes player minutes under a strict 240-minute
    team total constraint.

    Uses ``player_name`` as the natural primary key — each player appears
    at most once.  Subsequent syncs upsert (INSERT … ON CONFLICT UPDATE)
    to refresh status/reason without creating duplicates.

    Extended columns (added by InjurySyncService):
        team_id            — NBA team ID (int FK) for fast joins
        official_status    — raw status string from the NBA report
        effective_factor   — 0.00–1.00 minutes factor (includes DNP decay)
        consecutive_dnp_count — consecutive recent DNPs from game logs
    """

    __tablename__ = "nba_injuries"

    player_name = Column(String(128), primary_key=True)
    team_name = Column(String(64), nullable=False, index=True)
    team_id = Column(Integer, nullable=True, index=True)  # NBA team_id (nba_api)
    injury_status = Column(String(24), nullable=False)  # Out, Doubtful, Questionable, etc.
    official_status = Column(String(32), nullable=True)  # raw status from NBA report
    effective_factor = Column(Float, nullable=True, default=1.0)  # 0.00 – 1.00
    reason = Column(Text, nullable=True)
    consecutive_dnp_count = Column(Integer, nullable=True, default=0)
    last_updated = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_nba_inj_team", "team_name"),
        Index("ix_nba_inj_team_id", "team_id"),
        Index("ix_nba_inj_status", "injury_status"),
        Index("ix_nba_inj_factor", "effective_factor"),
    )

    def __repr__(self):
        return (
            f"<NBAInjury {self.player_name} "
            f"({self.team_name}) status={self.injury_status} "
            f"factor={self.effective_factor}>"
        )


# ──────────────────────────────────────────────────────────────────
# Solver Configuration & Result Tracking
# ──────────────────────────────────────────────────────────────────


class SolverConfiguration(Base):
    """A distinct set of optimizer parameters, deduplicated by content hash.

    Every unique combination of solver knobs gets one row.  The
    ``config_hash`` column stores a SHA-256 hex digest of the
    normalised parameter tuple so that repeated runs with the same
    settings reuse the existing row instead of creating duplicates.
    """

    __tablename__ = "solver_configurations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_hash = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(128), nullable=True)

    # Core solver knobs
    optimality_threshold = Column(Float, nullable=True)
    max_cumulative_ownership = Column(Float, nullable=True)
    global_max_exposure = Column(Float, nullable=True)
    salary_floor_pct = Column(Float, nullable=True)
    max_overlap = Column(Integer, nullable=True)

    # Strategy & contest context
    strategy = Column(String(32), nullable=True)
    contest_type = Column(String(16), nullable=True)
    platform = Column(String(4), nullable=True)
    sport = Column(String(4), nullable=True)

    # Randomness / reproducibility
    seed = Column(Integer, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    batches = relationship(
        "LineupBatch", back_populates="config", lazy="selectin",
    )

    __table_args__ = (
        Index("ix_sc_strategy_contest", "strategy", "contest_type"),
    )

    def __repr__(self):
        return (
            f"<SolverConfiguration id={self.id} "
            f"strategy={self.strategy} hash={self.config_hash[:12]}>"
        )


class LineupBatch(Base):
    """One invocation of ``generate_lineups()`` tied to a solver config.

    Captures the slate date, how many lineups were requested vs
    generated, and how long the solver took.
    """

    __tablename__ = "lineup_batches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_id = Column(
        Integer, ForeignKey("solver_configurations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    slate_date = Column(DateTime(timezone=True), nullable=True, index=True)
    draft_group_id = Column(Integer, nullable=True)
    total_lineups_requested = Column(Integer, nullable=False)
    total_lineups_generated = Column(Integer, nullable=False)
    generation_time_ms = Column(Integer, nullable=False)
    pool_size = Column(Integer, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    config = relationship(
        "SolverConfiguration", back_populates="batches",
    )
    results = relationship(
        "LineupResult", back_populates="batch",
        lazy="selectin", cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_lb_config_date", "config_id", "slate_date"),
    )

    def __repr__(self):
        return (
            f"<LineupBatch id={self.id} config={self.config_id} "
            f"generated={self.total_lineups_generated}/"
            f"{self.total_lineups_requested}>"
        )


class LineupResult(Base):
    """A single generated lineup within a batch.

    ``projected_score`` is written at generation time; ``actual_score``,
    ``entry_fee_total``, ``winnings_total``, and ``net_profit`` are
    back-filled after the contest settles.
    """

    __tablename__ = "lineup_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(
        Integer, ForeignKey("lineup_batches.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    lineup_hash = Column(String(64), nullable=False, index=True)

    # Projection data (written at generation)
    projected_score = Column(Float, nullable=False, default=0.0)
    total_salary = Column(Integer, nullable=True)
    quality_grade = Column(String(4), nullable=True)

    # Lineup composition (player IDs for quick lookup)
    player_ids = Column(JSONB, nullable=True)

    # Outcome data (back-filled after contest settles)
    actual_score = Column(Float, nullable=True)
    entry_fee_total = Column(Float, nullable=False, default=0.0)
    winnings_total = Column(Float, nullable=False, default=0.0)
    net_profit = Column(Float, nullable=False, default=0.0)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    batch = relationship("LineupBatch", back_populates="results")

    __table_args__ = (
        Index("ix_lr_batch_hash", "batch_id", "lineup_hash"),
    )

    def __repr__(self):
        return (
            f"<LineupResult id={self.id} batch={self.batch_id} "
            f"proj={self.projected_score:.1f} actual={self.actual_score}>"
        )


# ------------------------------------------------------------------
# Active entries — imported DraftKings contest entries for late-swap
# ------------------------------------------------------------------

class ActiveEntry(Base):
    """A user's live DraftKings contest entry, imported from edit_entries.csv.

    Each row represents one entry in one contest.  The ``lineup`` JSONB
    column stores the 8-slot roster as a list of dicts::

        [{"slot": "PG", "dk_player_id": 12345, "player_name": "...",
          "salary": 10400, "team": "DAL"}, ...]

    The DK ``entry_id`` is the natural unique key — re-uploading the
    same CSV upserts (updates lineup data) without creating duplicates.
    """

    __tablename__ = "active_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entry_id = Column(String(32), nullable=False, unique=True, index=True)
    contest_id = Column(String(32), nullable=False, index=True)
    contest_name = Column(String(256), nullable=True)
    draft_group_id = Column(Integer, nullable=False, index=True)
    fee = Column(Float, nullable=True)
    lineup = Column(JSONB, nullable=False)
    total_salary = Column(Integer, nullable=True)
    sport = Column(String(3), nullable=False, default="nba")
    imported_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_ae_draft_group", "draft_group_id"),
        Index("ix_ae_contest", "contest_id"),
        Index("ix_ae_dg_contest", "draft_group_id", "contest_id"),
    )

    def __repr__(self):
        return f"<ActiveEntry entry_id={self.entry_id} contest={self.contest_id}>"


class CBBPlayerGameLog(Base):
    """Cached CBB player box-score row from CBBpy.

    Populated by the daily background cache refresh job (4:30 AM ET).
    The CBB lineup optimizer reads from this table instead of live-scraping
    30+ teams via CBBpy, reducing pool build time from ~110s to <3s.
    """

    __tablename__ = "cbb_player_game_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_name = Column(String(128), nullable=False)
    team_name = Column(String(128), nullable=False)   # CBBpy team name
    team_id = Column(Integer, nullable=True)           # ESPN team ID
    season = Column(String(4), nullable=False)         # e.g. "2025"
    game_date = Column(DateTime(timezone=True), nullable=True)

    # Core box-score stats (matches CBBpy column output)
    min = Column(Float, nullable=True)
    pts = Column(Float, nullable=True)
    reb = Column(Float, nullable=True)
    ast = Column(Float, nullable=True)
    stl = Column(Float, nullable=True)
    blk = Column(Float, nullable=True)
    tov = Column(Float, nullable=True)
    fg3m = Column(Float, nullable=True)

    # Extended shooting splits
    fg3a = Column(Float, nullable=True)
    fgm = Column(Float, nullable=True)
    fga = Column(Float, nullable=True)
    ftm = Column(Float, nullable=True)
    fta = Column(Float, nullable=True)
    oreb = Column(Float, nullable=True)
    dreb = Column(Float, nullable=True)
    pf = Column(Float, nullable=True)

    game_id = Column(String(32), nullable=True)  # CBBpy game identifier
    fetched_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "player_name", "team_name", "season", "game_id",
            name="uq_cbb_pgl_player_game",
        ),
        Index("ix_cbb_pgl_team_season", "team_name", "season"),
        Index("ix_cbb_pgl_team_id_season", "team_id", "season"),
        Index("ix_cbb_pgl_player_season", "player_name", "season"),
    )

    def __repr__(self):
        return (
            f"<CBBPlayerGameLog {self.player_name} "
            f"({self.team_name}) {self.game_date}>"
        )


class GLeagueCache(Base):
    """Cached G-League player stats for two-way/call-up FPPM translation.

    When an NBA player has zero NBA game logs, the rotation engine can
    use their G-League production (with a 0.75x translation tax) as a
    better FPPM estimate than a blind positional baseline.

    Data source: ``nba_api`` with ``LeagueID="20"`` (NBA G-League).
    Refreshed during the 4 AM background job alongside NBA stats.
    """

    __tablename__ = "gleague_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Normalized player name (via normalize_player_name) — primary lookup key
    player_name_normalized = Column(
        String(128), nullable=False, unique=True, index=True,
    )
    # Original display name for logging/debugging
    player_name_display = Column(String(128), nullable=False)

    # G-League stats
    gleague_fppm = Column(Float, nullable=False)        # Raw G-League FPPM
    nba_translated_fppm = Column(Float, nullable=False)  # gleague_fppm * 0.75
    gleague_minutes = Column(Float, nullable=False)       # Total G-League minutes
    gleague_games = Column(Integer, nullable=False)        # Games played
    gleague_team = Column(String(64), nullable=True)       # G-League team name

    # Per-minute rates (for granular stat injection into PlayerMinutes)
    pts_per_min = Column(Float, nullable=True)
    reb_per_min = Column(Float, nullable=True)
    ast_per_min = Column(Float, nullable=True)
    stl_per_min = Column(Float, nullable=True)
    blk_per_min = Column(Float, nullable=True)
    tov_per_min = Column(Float, nullable=True)
    fg3m_per_min = Column(Float, nullable=True)

    # NBA season this G-League data corresponds to
    season = Column(String(10), nullable=False)  # e.g. "2025-26"

    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_gleague_cache_season", "season"),
    )

    def __repr__(self):
        return (
            f"<GLeagueCache {self.player_name_display} "
            f"FPPM={self.gleague_fppm:.3f} → {self.nba_translated_fppm:.3f}>"
        )
