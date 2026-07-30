from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from datetime import datetime


class TeamGameStats(BaseModel):
    """Season-level pace and scoring stats for a team."""
    team_id: int
    team_name: str
    team_abbreviation: str
    season_pace: float          # possessions per 48 min
    season_off_rating: float    # points per 100 possessions
    season_def_rating: float    # points allowed per 100 possessions
    season_ppg: float           # points per game
    season_opp_ppg: float       # opponent points per game
    last_5_ppg: float           # scoring trend last 5 games

    # Win-loss record (for competitive context classification)
    wins: int = 0
    losses: int = 0

    # Opponent stats allowed per game (for DvP matchup adjustments)
    opp_pts_pg: float = 0.0     # opponent points per game allowed
    opp_reb_pg: float = 0.0     # opponent rebounds per game allowed
    opp_ast_pg: float = 0.0     # opponent assists per game allowed
    opp_stl_pg: float = 0.0     # opponent steals per game allowed
    opp_blk_pg: float = 0.0     # opponent blocks per game allowed
    opp_tov_pg: float = 0.0     # opponent turnovers forced per game
    opp_fg3m_pg: float = 0.0    # opponent three-pointers made per game allowed

    @property
    def win_pct(self) -> float:
        """Win percentage (0.0-1.0). Returns 0.5 if no games played."""
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.5


class GameInfo(BaseModel):
    """Full game-day projection for a single matchup."""
    game_id: str
    game_date: str
    game_time_et: Optional[str] = None
    game_sequence: int = 0      # NBA API sequence (ordered by tip-off)
    game_status: str            # "Scheduled", "In Progress", "Final"

    # Teams
    home_team: TeamGameStats
    away_team: TeamGameStats

    # Projected totals
    projected_total: float      # projected combined score
    projected_home_score: float
    projected_away_score: float
    projected_spread: float     # home favored = negative

    # Pace
    projected_pace: float       # estimated possessions this game
    pace_label: str             # "Fast", "Average", "Slow"

    # Vegas lines (if available)
    over_under: Optional[float] = None      # Vegas total line
    over_under_edge: Optional[float] = None # projected_total - over_under
    vegas_spread: Optional[float] = None    # Vegas spread (home-relative, negative = home favored)

    # Schedule / fatigue context (populated by teams router)
    rest_days: Optional[int] = None             # Days since last game (0=B2B, 1=normal, 2+=rested)
    games_in_last_n_days: Optional[int] = None  # Games played in last 6 days (excl. today)

    # Future stub fields
    competitive_context: Optional[str] = None   # "playoff_push", "tanking", "clinched", None
    opponent_coach_id: Optional[int] = None     # Opponent's coach team_id for matchup lookup

    # Pre-computed game context modifiers (from Line Movement Agent live odds)
    context_modifier: Optional[Any] = None

    # Venue / stadium / arena name. Populated by sports whose schedule
    # feed exposes it (MLB ESPN scoreboard ships ``venue.fullName``).
    # Used downstream for park-factor adjustments (MLB) and home-court
    # context (NBA/CBB) once those engines come online.
    venue: Optional[str] = None

    # Live weather for outdoor MLB games (Prompt 4.3). Schema:
    #   temp           : float, °F at first pitch
    #   wind_speed     : float, mph (for use with ``calculate_wind_multiplier``)
    #   wind_direction : float, compass bearing (degrees the wind is BLOWING TOWARD)
    #   condition      : str — "Outdoor" for live-fetched values,
    #                    "Dome" for closed-roof venues (synthetic defaults),
    #                    other strings reserved for future Open-Meteo
    #                    weathercode mappings (e.g. "Rain", "Thunderstorm").
    # ``None`` means no weather was attached (non-MLB sport, missing
    # venue, or weather-fetch failure). Callers must null-check.
    weather: Optional[Dict[str, Any]] = None


class DFSSlate(BaseModel):
    """A time-based DFS slate grouping (Early, Main, Night)."""
    name: str               # "Early", "Main", "Night", "All Games"
    label: str              # "Early (7:00 PM)", "Main (7:00 PM+)", etc.
    game_count: int
    games: List[GameInfo]
    draft_group_id: Optional[int] = None  # DK DraftGroup ID for salary lookup

    # Live / late-swap status — computed from game statuses in _build_slates()
    is_live: bool = False           # True if any game in slate is "In Progress"
    late_swap_active: bool = False  # True if live AND has a valid draft_group_id


class Schedule(BaseModel):
    """Full slate of games for a given date."""
    date: str
    game_count: int
    games: List[GameInfo]
    slates: List[DFSSlate] = []  # games grouped by DFS slate times
