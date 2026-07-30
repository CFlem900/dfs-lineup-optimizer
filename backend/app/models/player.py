from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal
from datetime import datetime

# Maximum minutes a player can play (regulation + OT buffer)
_MAX_PLAYER_MINUTES = 53.0


class PlayerStatus(BaseModel):
    player_id: int
    player_name: str
    status: Literal["Available", "Out", "Questionable", "Doubtful", "GTD", "Game Time Decision"]
    injury_description: Optional[str] = None
    last_updated: datetime = Field(default_factory=datetime.now)


class PlayerMinutes(BaseModel):
    player_id: int
    player_name: str
    position: str
    team_id: int
    minutes_last_5: list[float]
    minutes_last_10: list[float]
    season_avg: float
    usage_rate: float
    age: Optional[int] = None  # Player age (for B2B fatigue adjustments)
    # Number of consecutive recent team games where the player logged 0 min.
    # Counts from the most recent game backwards, stopping at the first
    # game played.  Used to detect suspended/inactive players whose
    # season_avg is still high but who haven't played in weeks.
    recent_dnp_streak: int = 0
    # True if player was added via trade detection (DK lists them on a
    # team whose NBA API roster doesn't include them yet).  Used by the
    # rotation engine to flag projections as having limited same-team data.
    roster_change_detected: bool = False
    # DK salary (populated from draftable_salaries during rotation build).
    # Used by the rotation engine's auto-out logic: high-salary players
    # with short DNP streaks are exempt from auto-out (DK wouldn't price
    # them high if they're not expected to play).
    dk_salary: Optional[int] = None
    # DK position (e.g. "PG", "SG/SF") from the draftables feed.
    # More specific than BDL positions ("G", "F") and reflects DK's
    # current categorisation of the player's expected role.
    dk_position: Optional[str] = None
    # Market-derived ownership % from external Data Hub CSV import.
    # Used by Phase 0d Chalk Override in allocate_team_minutes() to promote
    # high-ownership bench players to starters.  None = no external signal.
    market_ownership: Optional[float] = None
    # True when an external data source (Data Hub CSV) confirms this player
    # is starting tonight.  Used by Chalk Override to pre-seed starters.
    is_confirmed_starter: Optional[bool] = None
    # True when DK pricing anomaly detection flags this player as a
    # situational starter: salary > $3,000 (above minimum) but < 5 games
    # in the local BDL database.  DK's pricing algorithms reveal role
    # expectations before news breaks.  Used by TopDownMinutes to reserve
    # a 24-minute baseline before standard distribution.
    is_situational_starter: Optional[bool] = None
    # Vegas-derived implied minutes from The Odds API PRA prop line.
    # When a sportsbook posts a PRA prop for a fringe player (salary ≤ $4,500),
    # it signals a confirmed role.  The implied minutes override the BDL
    # database's 0.0 projection for G-League call-ups and deep bench players.
    vegas_implied_minutes: Optional[float] = None
    # True when a player is confirmed active by Vegas PRA prop existence
    # AND meets the salary threshold (≤ $4,500).  Used by TopDownMinutes
    # to forcefully assign a baseline before standard distribution.
    vegas_confirmed: Optional[bool] = None
    # True when a beat reporter tweet/alert confirms this player is starting
    # (e.g., "[Underdog NBA] Bez Mbeng will start in place of Ja Morant").
    # Detected by NewsParserService NLP scanner.  Treated like is_confirmed_starter
    # but with a 30-minute baseline and synthetic FPPM of 0.85.
    news_confirmed_starter: Optional[bool] = None
    # Per-minute stat rates (season averages for DFS projections)
    pts_per_min: float = 0.0
    reb_per_min: float = 0.0
    ast_per_min: float = 0.0
    stl_per_min: float = 0.0
    blk_per_min: float = 0.0
    tov_per_min: float = 0.0
    fg3m_per_min: float = 0.0

    # Shooting profile for shot-type decomposition (optional).
    # When available, PTS projection is decomposed into fg3m×3 + fg2m×2 + ftm,
    # making the FG3M DK bonus naturally correlated with scoring volume.
    fg3a_rate: Optional[float] = None    # 3PT attempts per minute
    fga_rate: Optional[float] = None     # Total FG attempts per minute
    fta_rate: Optional[float] = None     # Free-throw attempts per minute
    fg3_pct: Optional[float] = None      # 3PT percentage (0.0 - 1.0)
    fg2_pct: Optional[float] = None      # 2PT percentage (0.0 - 1.0)
    ft_pct: Optional[float] = None       # FT percentage (0.0 - 1.0)

    @property
    def has_shooting_profile(self) -> bool:
        """True if enough shooting profile data is available for decomposition."""
        return (
            self.fg3a_rate is not None
            and self.fga_rate is not None
            and self.fta_rate is not None
            and self.fg3_pct is not None
            and self.fg2_pct is not None
            and self.ft_pct is not None
        )

    @property
    def minutes_cv(self) -> float:
        """Coefficient of variation of recent minutes (proxy for game-to-game volatility).

        Uses last-10 game minutes when available; falls back to last-5.
        Returns 0.30 (league average) when insufficient data.
        """
        games = self.minutes_last_10 if len(self.minutes_last_10) >= 5 else self.minutes_last_5
        if len(games) < 3 or self.season_avg <= 0:
            return 0.30  # League-average fallback
        import statistics
        mean = statistics.mean(games)
        if mean <= 0:
            return 0.30
        stdev = statistics.stdev(games)
        return min(stdev / mean, 1.0)  # Cap at 1.0


class PlayerProjection(BaseModel):
    player_id: int
    player_name: str
    position: str
    baseline_minutes: float
    adjusted_minutes: float
    confidence: float
    reason: Optional[str] = None

    @field_validator("adjusted_minutes")
    @classmethod
    def cap_adjusted_minutes(cls, v: float) -> float:
        """Safety net: no player can play more than 53 minutes (regulation + OT)."""
        return min(max(v, 0.0), _MAX_PLAYER_MINUTES)
    # DFS projections (populated by DFSService)
    dk_points: Optional[float] = None
    fd_points: Optional[float] = None
    dk_floor: Optional[float] = None
    dk_ceiling: Optional[float] = None
    fd_floor: Optional[float] = None
    fd_ceiling: Optional[float] = None
    projected_stats: Optional[dict] = None
    usage_boost: float = 1.0               # Per-minute rate multiplier from teammate injuries
    pace_factor: float = 1.0              # Game pace multiplier for DFS stat scaling
    garbage_time_factor: float = 1.0      # Per-minute rate discount for bench garbage time (< 1.0 = reduced quality)
    roster_change_detected: bool = False  # True if team traded for/away a key player recently
    is_spot_starter: bool = False         # True if promoted to starting role due to injury
    coach_decision_dnp: bool = False      # True if zeroed by active-depth filter
    play_probability: float = 1.0         # P(plays) — for Questionable=0.85, GTD=0.72, etc.
    dag_redistributions: list = Field(default_factory=list)  # [{from_player, minutes, cascade_target, role, ceiling}]
    # DFS salary data (populated by DKDraftablesService)
    dk_salary: Optional[int] = None       # DraftKings salary in dollars (e.g., 8500)
    dk_value: Optional[float] = None      # Projected FP per $1K (e.g., 5.2)
    fd_salary: Optional[int] = None       # FanDuel salary estimate (DK salary × 1.2)
    fd_value: Optional[float] = None      # FD projected FP per $1K
