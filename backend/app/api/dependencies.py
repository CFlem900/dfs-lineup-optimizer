"""Centralised service container for the RotationEngine API.

All service and agent instantiation lives here so that router modules
can access them via ``get_services()`` without circular imports.

Usage in any router::

    from app.api.dependencies import get_services

    svc = get_services()
    svc.nba_service.get_all_teams()
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ServiceContainer:
    """Lazy-initialised holder for every service and agent instance."""

    def __init__(self):
        self._initialised = False

    def _ensure_init(self):
        if self._initialised:
            return
        self._initialised = True
        self._init_services()

    # ------------------------------------------------------------------
    # Private: wire up everything in the correct dependency order
    # ------------------------------------------------------------------
    def _init_services(self):
        # ── Core services ─────────────────────────────────────────────
        from app.services.nba_api_service import NBAApiService
        from app.services.injury_service import InjuryService
        from app.services.game_service import GameService
        from app.services.dfs_service import DFSService
        from app.services.dk_draftables_service import DKDraftablesService
        from app.services.dk_props_service import DKPropsService
        from app.services.dk_available_players_service import DKAvailablePlayersService
        from app.services.dk_contest_detail_service import DKContestDetailService
        from app.services.news_service import NewsService
        from app.services.simulation_engine import SimulationEngine

        self.injury_service = InjuryService()
        self.game_service = GameService()
        self.dfs_service = DFSService()  # calibration + props injected below
        self.dk_draftables_service = DKDraftablesService()
        self.dk_props_service = DKPropsService()
        self.dk_available_players_service = DKAvailablePlayersService()
        self.dk_contest_detail_service = DKContestDetailService()
        self.news_service = NewsService()

        # ── NBA Data Cache (PostgreSQL cache for NBA API data) ────
        from app.services.nba_data_cache_service import NBADataCacheService
        self.nba_data_cache = NBADataCacheService()

        # ── Multi-source NBA data (BDL primary → nba_api fallback) ──
        from app.services.balldontlie_service import BallDontLieService
        from app.services.nba_multi_source import NBAMultiSourceService
        from app.config import get_settings as _get_bdl_settings
        _bdl_settings = _get_bdl_settings()

        _raw_nba = NBAApiService()
        _bdl = BallDontLieService() if _bdl_settings.balldontlie_api_key else None
        self.bdl_service = _bdl  # Exposed for late-swap game-status queries
        self.nba_service = NBAMultiSourceService(
            nba_api=_raw_nba,
            balldontlie=_bdl,
            db_cache=self.nba_data_cache,
        )
        if _bdl and _bdl.is_available:
            logger.info("[Init] BallDontLie API configured — primary NBA data source")
        else:
            logger.info("[Init] BallDontLie API not configured — using stats.nba.com only")

        # Inject DB cache into game_service (reads team stats from DB)
        self.game_service._db_cache = self.nba_data_cache
        # Inject multi-source data service for BDL-first scoreboard
        self.game_service._data_service = self.nba_service
        # Inject BDL for odds fallback (NBA live odds endpoint is dead)
        self.game_service._bdl = _bdl

        # ── CBB services ───────────────────────────────────────────────
        from app.services.cbb_api_service import CBBApiService
        from app.services.cbb_game_service import CBBGameService
        from app.services.cbb_injury_service import CBBInjuryService
        from app.services.cbb_stats_service import CBBStatsService
        from app.services.odds_service import OddsService

        from app.services.cbb_props_service import CBBPropsService

        self.cbb_service = CBBApiService()
        self.cbb_stats_service = CBBStatsService()
        self.cbb_props_service = CBBPropsService()
        from app.config import get_settings as _get_settings
        _settings = _get_settings()
        self.odds_service = OddsService(
            api_key=getattr(_settings, "odds_api_key", ""),
        )
        # Inject The Odds API into game_service for NBA odds fallback
        self.game_service._odds_service = self.odds_service

        # Inject unified OddsFetcherService (API → BDL → heuristic)
        from app.services.odds_fetcher_service import OddsFetcherService
        self.odds_fetcher = OddsFetcherService(
            odds_api_key=getattr(_settings, "odds_api_key", ""),
            bdl_service=_bdl,
        )
        self.game_service._odds_fetcher = self.odds_fetcher

        # Vegas player prop → implied minutes service
        from app.services.vegas_player_props_service import VegasPlayerPropsService
        self.vegas_player_props_service = VegasPlayerPropsService(
            api_key=getattr(_settings, "odds_api_key", ""),
        )

        self.cbb_game_service = CBBGameService(
            stats_service=self.cbb_stats_service,
            odds_service=self.odds_service,
        )
        self.cbb_injury_service = CBBInjuryService()

        # ── NFL services ──────────────────────────────────────────────
        # Data + Game are real (Prompt 1.4): NFLDataService owns the 32-team
        # registry and ESPN-id translation; NFLGameService fetches live
        # schedules from ESPN's hidden scoreboard API. Injury + Props are
        # still skeletons returning empty results until we wire real feeds.
        from app.services.nfl_data_service import NFLDataService
        from app.services.nfl_game_service import NFLGameService
        from app.services.nfl_services import NFLInjuryService, NFLPropsService
        self.nfl_data_service = NFLDataService()
        self.nfl_game_service = NFLGameService(data_service=self.nfl_data_service)
        self.nfl_injury_service = NFLInjuryService()
        self.nfl_props_service = NFLPropsService()

        # ── MLB services ──────────────────────────────────────────────
        # Data + Game are real (Prompt 2.2): MLBDataService owns the 30-team
        # registry (with home_park metadata for park-factor work) and
        # ESPN-id translation; MLBGameService fetches live schedules from
        # ESPN's hidden scoreboard API and captures the per-game venue.
        # Injury + Props are still skeletons returning empty results.
        from app.services.mlb_data_service import MLBDataService
        from app.services.mlb_game_service import MLBGameService
        from app.services.mlb_services import MLBInjuryService, MLBPropsService
        self.mlb_data_service = MLBDataService()
        self.mlb_game_service = MLBGameService(data_service=self.mlb_data_service)
        self.mlb_injury_service = MLBInjuryService()
        self.mlb_props_service = MLBPropsService()

        # ── Underdog Fantasy services ──────────────────────────────────
        from app.services.underdog_api_service import UnderdogApiService
        from app.services.underdog_pickem_service import UnderdogPickemService

        self.underdog_api_service = UnderdogApiService()
        self.underdog_pickem_service = UnderdogPickemService(
            underdog_api=self.underdog_api_service,
            dfs_service=self.dfs_service,
        )

        # ── AI Service (central LLM abstraction) ─────────────────────
        from app.services.ai_service import AIService

        self.ai_service = AIService()

        # ── Feature Flags ──────────────────────────────────────────────
        from app.services.feature_flags import FeatureFlagService

        flags = FeatureFlagService()
        self.feature_flags = flags

        disabled = [k for k, v in flags.list_flags().items() if not v]
        if disabled:
            logger.info(f"[Init] Disabled features: {', '.join(disabled)}")

        # ── AI Agents (gated by feature flags) ─────────────────────────
        from app.services.agents.signal_analysis_agent import SignalAnalysisAgent
        from app.services.agents.injury_impact_agent import InjuryImpactAgent
        from app.services.agents.news_projection_agent import NewsProjectionAgent
        from app.services.agents.ownership_agent import OwnershipProjectionAgent
        from app.services.agents.lineup_strategy_agent import LineupStrategyAgent
        from app.services.agents.narrative_agent import NarrativeAgent
        from app.services.agents.simulation_tuning_agent import SimulationTuningAgent
        from app.services.agents.coach_learning_agent import CoachLearningAgent
        from app.services.agents.expert_quality_agent import ExpertQualityAgent
        from app.services.agents.backtesting_agent import BacktestingAgent
        from app.services.agents.tournament_analysis_agent import TournamentAnalysisAgent
        from app.services.agents.chat_agent import ChatAgent
        from app.services.agents.line_movement_agent import LineMovementAgent

        _ai = self.ai_service
        self.line_movement_agent = LineMovementAgent(_ai, bdl_service=_bdl) if flags.is_enabled('AGENT_LINE_MOVEMENT') else None
        self.signal_analysis_agent = SignalAnalysisAgent(_ai) if flags.is_enabled('AGENT_SIGNAL_ANALYSIS') else None
        self.injury_impact_agent = InjuryImpactAgent(_ai) if flags.is_enabled('AGENT_INJURY_IMPACT') else None
        self.news_projection_agent = NewsProjectionAgent(_ai) if flags.is_enabled('AGENT_NEWS_PROJECTION') else None
        self.ownership_agent = OwnershipProjectionAgent(_ai) if flags.is_enabled('AGENT_OWNERSHIP') else None
        self.lineup_strategy_agent = LineupStrategyAgent(_ai) if flags.is_enabled('AGENT_LINEUP_STRATEGY') else None
        self.narrative_agent = NarrativeAgent(_ai) if flags.is_enabled('AGENT_NARRATIVE') else None
        self.simulation_tuning_agent = SimulationTuningAgent(_ai) if flags.is_enabled('AGENT_SIMULATION_TUNING') else None
        self.coach_learning_agent = CoachLearningAgent(_ai) if flags.is_enabled('AGENT_COACH_LEARNING') else None
        self.expert_quality_agent = ExpertQualityAgent(_ai) if flags.is_enabled('AGENT_EXPERT_QUALITY') else None
        self.backtesting_agent = BacktestingAgent(_ai) if flags.is_enabled('AGENT_BACKTESTING') else None
        self.tournament_analysis_agent = TournamentAnalysisAgent(_ai) if flags.is_enabled('AGENT_TOURNAMENT_ANALYSIS') else None

        # ── Tournament + Calibration + Usage services ────────────────
        from app.services.ingestion.tournament_import_service import TournamentImportService
        from app.services.calibration_service import CalibrationService
        from app.services.ai_usage_service import AIUsageService
        from app.services.coach_profile_service import CoachProfileService
        from app.services.accuracy_service import AccuracyService
        from app.services.fade_service import FadeService
        from app.services.ownership_simulator import OwnershipSimulator
        from app.services.correlation_service import CorrelationService

        self.tournament_import_service = TournamentImportService()

        from app.services.entry_import_service import EntryImportService
        self.entry_import_service = EntryImportService()

        self.calibration_service = CalibrationService() if flags.is_enabled('CALIBRATION') else None
        self.ai_usage_service = AIUsageService()
        self.coach_profile_service = CoachProfileService()
        self.accuracy_service = AccuracyService()
        self.fade_service = FadeService()
        self.ownership_simulator = OwnershipSimulator()
        self.correlation_service = CorrelationService()

        from app.services.solver_tracking_service import SolverTrackingService
        self.solver_tracking_service = SolverTrackingService()

        # ── Contest Recommender ────────────────────────────────────────
        from app.services.contest_recommender_service import ContestRecommenderService

        self.contest_recommender_service = ContestRecommenderService(
            dk_contest_detail_service=self.dk_contest_detail_service,
            calibration_service=self.calibration_service,
        )

        # ── Pre-Lock Polling Service ─────────────────────────────────
        from app.services.pre_lock_polling_service import PreLockPollingService

        self.pre_lock_polling_service = PreLockPollingService(
            injury_service=self.injury_service,
            news_service=self.news_service,
            cache_service=None,  # Injected in main.py after cache connects
        )

        # ── Player ID Mapper (DK ↔ BDL) ─────────────────────────────
        from app.services.player_id_mapper import PlayerIdMapper

        self.player_id_mapper = PlayerIdMapper(
            bdl_service=_bdl,
            cache_service=None,  # Injected in main.py after cache connects
        ) if _bdl else None

        # ── Blowout Risk Analyzer ────────────────────────────────────
        from app.services.blowout_risk_analyzer import BlowoutRiskAnalyzer

        self.blowout_risk_analyzer = BlowoutRiskAnalyzer(
            bdl_service=_bdl,
        ) if _bdl else None

        # ── BDL MCP Client + ContextualRefiner ──────────────────────
        from app.services.bdl_mcp_client import BDLMCPClient
        from app.services.prop_edge_analyzer import ContextualRefiner

        self.bdl_mcp_client = BDLMCPClient(
            bdl_service=_bdl,
            injury_service=self.injury_service,
        ) if _bdl else None

        self.contextual_refiner = ContextualRefiner(
            bdl_mcp_client=self.bdl_mcp_client,
        ) if self.bdl_mcp_client else None

        # ── Garbage Time Opportunity Detector ─────────────────────────
        from app.services.prop_edge_analyzer import GarbageTimeOpportunity

        self.garbage_time_detector = GarbageTimeOpportunity(
            bdl_mcp_client=self.bdl_mcp_client,
            bdl_service=_bdl,
            contextual_refiner=self.contextual_refiner,
        ) if (self.bdl_mcp_client and _bdl) else None

        # ── Live Prop Tracker ─────────────────────────────────────────
        from app.services.live_game_state_service import LiveGameStateService
        from app.services.live_prop_tracker_service import LivePropTrackerService

        self.live_game_state_service = LiveGameStateService()
        self.live_prop_tracker_service = LivePropTrackerService(
            dk_props_service=self.dk_props_service,
            live_game_state_service=self.live_game_state_service,
            cache_service=None,  # Injected in main.py after cache connects
            bdl_service=_bdl,
        )

        # ── Late-Swap Service (ILP re-optimiser for imported entries) ──
        from app.services.late_swap_service import LateSwapService
        self.late_swap_service = LateSwapService(
            live_game_state_service=self.live_game_state_service,
            entry_import_service=self.entry_import_service,
        )

        # Inject calibration and props into DFS service (created earlier without them)
        self.dfs_service._calibration = self.calibration_service
        self.dfs_service._props_service = self.dk_props_service

        # ── G-League stats service (zero-history FPPM translation) ──
        from app.services.gleague_stats_service import GLeagueStatsService

        self.gleague_service = GLeagueStatsService(db_pool=getattr(self, "_db_pool", None))

        # ── Core engine with AI agents injected ──────────────────────
        from app.services.rotation_engine import RotationEngine

        self.engine = RotationEngine(
            injury_impact_agent=self.injury_impact_agent,
            coach_learning_agent=self.coach_learning_agent,
            calibration_service=self.calibration_service,
            gleague_service=self.gleague_service,
        )

        # ── Services with AI agent injection ─────────────────────────
        from app.services.expert_signal_service import ExpertSignalService
        from app.services.lineup_optimizer_service import LineupOptimizerService
        from app.services.lineup_analysis_service import LineupAnalysisService

        self.expert_signal_service = ExpertSignalService(
            signal_analysis_agent=self.signal_analysis_agent,
            expert_quality_agent=self.expert_quality_agent,
        )
        self.lineup_optimizer_service = LineupOptimizerService(
            dfs_service=self.dfs_service,
            dk_draftables_service=self.dk_draftables_service,
            nba_service=self.nba_service,
            injury_service=self.injury_service,
            rotation_engine=self.engine,
            simulation_engine=SimulationEngine(),
            expert_signal_service=self.expert_signal_service,
            game_service=self.game_service,
            news_projection_agent=self.news_projection_agent,
            ownership_agent=self.ownership_agent,
            lineup_strategy_agent=self.lineup_strategy_agent,
            simulation_tuning_agent=self.simulation_tuning_agent,
            calibration_service=self.calibration_service,
            correlation_service=self.correlation_service,
            dk_props_service=self.dk_props_service,
            vegas_player_props_service=self.vegas_player_props_service,
            dk_available_players_service=self.dk_available_players_service,
            fade_service=self.fade_service,
            cbb_data_service=self.cbb_service,
            cbb_game_service=self.cbb_game_service,
            cbb_injury_service=self.cbb_injury_service,
            line_movement_agent=self.line_movement_agent,
            solver_tracking_service=self.solver_tracking_service,
        )

        # Inject the news parser, news service, and Discord fetcher for beat reporter NLP
        from app.services.news_parser_service import NewsParserService
        from app.services.discord_news_service import DiscordNewsService
        self.lineup_optimizer_service._news_parser = NewsParserService()
        self.lineup_optimizer_service._news_service = getattr(self, "news_service", None)
        self.lineup_optimizer_service._discord_news_service = DiscordNewsService()
        self.lineup_analysis_service = LineupAnalysisService(
            game_service=self.game_service,
            expert_signal_service=self.expert_signal_service,
            injury_service=self.injury_service,
            lineup_optimizer_service=self.lineup_optimizer_service,
            narrative_agent=self.narrative_agent,
        )
        self.chat_agent = ChatAgent(
            ai_service=self.ai_service,
            lineup_optimizer=self.lineup_optimizer_service,
            expert_signal_service=self.expert_signal_service,
            game_service=self.game_service,
            injury_service=self.injury_service,
        ) if flags.is_enabled('AGENT_CHAT') else None

        # ── Simulate-and-Filter pipeline ─────────────────────────────
        from app.services.simulate_and_filter_service import SimulateAndFilterService

        self.sim_filter_service = SimulateAndFilterService(
            optimizer=self.lineup_optimizer_service,
        )

        # ── Historical Backfill ──────────────────────────────────────
        from app.services.ingestion.historical_backfill import HistoricalBackfill

        self.historical_backfill = HistoricalBackfill(
            nba_service=self.nba_service,
            rotation_engine=self.engine,
            dfs_service=self.dfs_service,
            game_service=self.game_service,
            injury_service=self.injury_service,
        )

        # ── Late-Swap Monitor ────────────────────────────────────────
        from app.services.late_swap_monitor import LateSwapMonitor

        self.late_swap_monitor = LateSwapMonitor(
            injury_service=self.injury_service,
            game_service=self.game_service,
            news_service=self.news_service,
            lineup_optimizer_service=self.lineup_optimizer_service,
            dk_draftables_service=self.dk_draftables_service,
        )

        # ── Box Score Ingester ───────────────────────────────────────
        from app.services.ingestion.box_score_ingester import BoxScoreIngester

        self.box_score_ingester = BoxScoreIngester(
            nba_service=self.nba_service,
            game_service=self.game_service,
            on_complete=self._post_ingestion_callback,
        )

        # ── Sport-aware service map ──────────────────────────────────
        # Built last so every service it references is already wired.
        # All four ``get_*_service`` methods read from this map.
        self._sport_services: Dict[str, Dict[str, Any]] = self._build_sport_service_map()
        logger.info(
            "[ServiceContainer] Sport service map registered for: %s",
            sorted(self._sport_services.keys()),
        )

    # ------------------------------------------------------------------
    # Post-ingestion callback (was a module-level function in routes.py)
    # ------------------------------------------------------------------
    async def _post_ingestion_callback(self, game_date: str, rows_inserted: int):
        """Auto-trigger projection analysis after box score ingestion."""
        try:
            logger.info(
                f"[Ingest] Post-ingestion callback: {rows_inserted} rows for "
                f"{game_date}, running projection analysis..."
            )
            result = await run_projection_analysis(days=30)
            if result:
                logger.info(
                    f"[Ingest] Auto-analysis complete: "
                    f"{result.get('calibrations_saved', 0)} calibrations saved"
                )
        except Exception as exc:
            logger.warning(f"[Ingest] Post-ingestion analysis failed: {exc}")

    # ------------------------------------------------------------------
    # Sport-aware service accessors (registry-backed)
    # ------------------------------------------------------------------
    # The four getters below resolve via ``self._sport_services`` — a
    # nested dict populated at the end of ``_init_services``. Adding a
    # new sport now means: register a SportConfig in ``app.sports``,
    # build skeleton services, add an entry here. No more hardcoded
    # ``if sport == "cbb"`` ternaries scattered through the codebase.

    def _build_sport_service_map(self) -> Dict[str, Dict[str, Any]]:
        """Construct the {sport_code: {role: service}} lookup table.

        Called once from ``_init_services`` after every concrete service
        is instantiated. Roles: 'data', 'game', 'injury', 'props'. NFL
        and MLB are skeletons — see ``nfl_services.py`` / ``mlb_services.py``.
        """
        return {
            "nba": {
                "data":   self.nba_service,
                "game":   self.game_service,
                "injury": self.injury_service,
                "props":  self.dk_props_service,
            },
            "cbb": {
                "data":   self.cbb_service,
                "game":   self.cbb_game_service,
                "injury": self.cbb_injury_service,
                "props":  self.cbb_props_service,
            },
            "nfl": {
                "data":   self.nfl_data_service,
                "game":   self.nfl_game_service,
                "injury": self.nfl_injury_service,
                "props":  self.nfl_props_service,
            },
            "mlb": {
                "data":   self.mlb_data_service,
                "game":   self.mlb_game_service,
                "injury": self.mlb_injury_service,
                "props":  self.mlb_props_service,
            },
        }

    def _resolve_sport_service(self, sport: str, role: str):
        """Look up a (sport, role) service. Falls back to NBA on unknown
        sport so the API stays soft-failing for typos rather than
        crashing — but logs a warning so we notice."""
        self._ensure_init()
        smap = getattr(self, "_sport_services", None)
        if smap is None:
            # Should never happen — _init_services populates it. Defensive.
            self._sport_services = self._build_sport_service_map()
            smap = self._sport_services
        sport_key = (sport or "nba").lower()
        services = smap.get(sport_key)
        if services is None:
            logger.warning(
                "[ServiceContainer] Unknown sport %r requested for role %r; "
                "falling back to NBA. Registered sports: %s",
                sport_key, role, sorted(smap.keys()),
            )
            services = smap["nba"]
        svc = services.get(role)
        if svc is None:
            raise ValueError(
                f"Sport {sport_key!r} has no service registered for role {role!r}. "
                f"Available roles: {sorted(services.keys())}"
            )
        return svc

    def get_data_service(self, sport: str = "nba"):
        return self._resolve_sport_service(sport, "data")

    def get_game_service(self, sport: str = "nba"):
        return self._resolve_sport_service(sport, "game")

    def get_injury_service(self, sport: str = "nba"):
        return self._resolve_sport_service(sport, "injury")

    def get_props_service(self, sport: str = "nba"):
        return self._resolve_sport_service(sport, "props")

    # ------------------------------------------------------------------
    # Attribute access triggers lazy init
    # ------------------------------------------------------------------
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self._ensure_init()
        return object.__getattribute__(self, name)


# ── Module-level singleton ────────────────────────────────────────────
services = ServiceContainer()


def get_services() -> ServiceContainer:
    """Return the module-level ServiceContainer (lazy-initialised)."""
    services._ensure_init()
    return services


# ── Shared helper: projection analysis ────────────────────────────────
async def run_projection_analysis(days: int = 30, sport: str = "nba") -> Optional[Dict]:
    """Run projection accuracy analysis and save calibrations.

    Used by both the /backtest/analysis endpoint and the post-ingestion
    auto-trigger.  Returns dict with analysis results or None.
    """
    from sqlalchemy import select
    from app.db.models import PlayerMinutesHistory
    from app.db.database import is_db_available, get_session

    svc = get_services()

    if not is_db_available():
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with get_session() as session:
        stmt = (
            select(PlayerMinutesHistory)
            .where(PlayerMinutesHistory.actual_minutes.isnot(None))
            .where(PlayerMinutesHistory.game_date >= cutoff)
            .order_by(PlayerMinutesHistory.game_date.desc())
            .limit(500)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    if not rows:
        return None

    # Build a set of (team_id, date) pairs to detect back-to-backs.
    team_dates: set = set()
    for r in rows:
        if r.team_id and r.game_date:
            gd = r.game_date.date() if hasattr(r.game_date, "date") else r.game_date
            team_dates.add((r.team_id, gd))

    def _is_b2b(team_id, game_date) -> bool:
        if not team_id or not game_date:
            return False
        gd = game_date.date() if hasattr(game_date, "date") else game_date
        prev_day = gd - timedelta(days=1)
        return (team_id, prev_day) in team_dates

    # Build enriched accuracy data with per-stat breakdowns
    accuracy_data = []
    for r in rows:
        dk_salary = r.dk_salary or 0
        if dk_salary >= 8000:
            tier = "high"
        elif dk_salary >= 5000:
            tier = "mid"
        else:
            tier = "value"

        accuracy_data.append({
            "player_name": r.player_name,
            "position": r.position,
            "team_id": r.team_id,
            "dk_salary": dk_salary,
            "salary_tier": tier,
            "projected_minutes": r.projected_minutes or 0,
            "actual_minutes": r.actual_minutes or 0,
            "projected_fp": r.dk_projected_fp or 0,
            "actual_fp": r.dk_actual_fp or 0,
            "projected_pts": r.projected_pts or 0,
            "actual_pts": r.actual_pts or 0,
            "projected_reb": r.projected_reb or 0,
            "actual_reb": r.actual_reb or 0,
            "projected_ast": r.projected_ast or 0,
            "actual_ast": r.actual_ast or 0,
            "projected_stl": r.projected_stl or 0,
            "actual_stl": r.actual_stl or 0,
            "projected_blk": r.projected_blk or 0,
            "actual_blk": r.actual_blk or 0,
            "projected_tov": r.projected_tov or 0,
            "actual_tov": r.actual_tov or 0,
            "projected_fg3m": r.projected_fg3m or 0,
            "actual_fg3m": r.actual_fg3m or 0,
            "was_b2b": _is_b2b(r.team_id, r.game_date),
            "game_date": r.game_date.isoformat() if r.game_date else "",
            # Shot decomposition (populated by ingester from V3 box scores)
            "actual_fg3a_rate": getattr(r, "actual_fg3a_rate", None),
            "actual_fga_rate": getattr(r, "actual_fga_rate", None),
            "actual_fta_rate": getattr(r, "actual_fta_rate", None),
            "actual_fg3_pct": getattr(r, "actual_fg3_pct", None),
            "actual_fg2_pct": getattr(r, "actual_fg2_pct", None),
            "actual_ft_pct": getattr(r, "actual_ft_pct", None),
            "projected_fg3a_rate": getattr(r, "projected_fg3a_rate", None),
            "projected_fga_rate": getattr(r, "projected_fga_rate", None),
            "projected_fta_rate": getattr(r, "projected_fta_rate", None),
            "opponent_team_id": getattr(r, "opponent_team_id", None),
        })

    # Use enhanced analysis if per-stat data is available
    has_stat_data = any(
        r.get("actual_pts", 0) > 0 for r in accuracy_data
    )

    if has_stat_data:
        analysis = svc.backtesting_agent.analyze_projection_accuracy(
            accuracy_data, context={"period": f"last_{days}_days"}
        )
    else:
        analysis = svc.backtesting_agent.analyze_accuracy(
            accuracy_data, context={"period": f"last_{days}_days"}
        )

    if not analysis:
        # ── Deterministic fallback: Agent 9 unavailable ──────────────
        from app.services.agents.backtesting_agent import (
            compute_accuracy_stats,
            compute_deterministic_calibrations,
        )

        stats = compute_accuracy_stats(accuracy_data)
        det_adjustments = compute_deterministic_calibrations(accuracy_data)

        if det_adjustments:
            calibrations_saved = await svc.calibration_service.save_backtest_calibrations(
                det_adjustments,
                metadata={
                    "game_count": len(rows),
                    "source": "deterministic_fallback",
                    "reasoning": "Agent 9 unavailable; deterministic bias-based calibration",
                },
            )
            await svc.calibration_service.load_calibrations()
            logger.info(
                f"[Analysis] Deterministic fallback: {calibrations_saved} calibrations "
                f"saved (FP bias={stats.get('overall_fp_bias', 0):.2f})"
            )
            return {
                "analysis": {"source": "deterministic", "stats": stats},
                "calibrations_saved": calibrations_saved,
            }

        logger.info("[Analysis] No significant biases detected — no calibrations needed")
        return {"analysis": {"source": "deterministic", "stats": stats}, "calibrations_saved": 0}

    # ── AI path succeeded — save calibrations to DB ──────────────
    # Also compute deterministic stats for bias logging
    from app.services.agents.backtesting_agent import compute_accuracy_stats

    stats = compute_accuracy_stats(accuracy_data)

    calibrations_saved = 0
    if analysis.calibration_adjustments:
        calibrations_saved = await svc.calibration_service.save_backtest_calibrations(
            analysis.calibration_adjustments,
            metadata={
                "game_count": len(rows),
                "reasoning": "; ".join(analysis.recommendations[:3])
                if analysis.recommendations else "",
            },
        )
        # Refresh in-memory cache
        await svc.calibration_service.load_calibrations()

    # ── BDL advanced metric calibrations ──────────────────────────
    from app.services.agents.backtesting_agent import (
        compute_shot_decomposition_stats,
        compute_dvp_recalibration,
        compute_noise_sigma_adjustments,
        _shot_rate_to_calibration_keys,
    )

    has_bdl_data = any(
        r.get("actual_fg3a_rate") is not None for r in accuracy_data
    )

    if has_bdl_data:
        try:
            shot_stats = compute_shot_decomposition_stats(accuracy_data)
            dvp_adjustments = compute_dvp_recalibration(accuracy_data)
            sigma_adjustments = compute_noise_sigma_adjustments(accuracy_data)

            bdl_calibrations = {}
            bdl_calibrations.update(_shot_rate_to_calibration_keys(shot_stats))
            bdl_calibrations.update(dvp_adjustments)
            bdl_calibrations.update(sigma_adjustments)

            if bdl_calibrations:
                bdl_saved = await svc.calibration_service.save_backtest_calibrations(
                    bdl_calibrations,
                    metadata={
                        "game_count": len(rows),
                        "source": "bdl_advanced_metrics",
                        "reasoning": (
                            "Shot decomposition + DvP + noise sigma "
                            "recalibration"
                        ),
                    },
                )
                calibrations_saved += bdl_saved
                await svc.calibration_service.load_calibrations()
                logger.info(
                    f"[Analysis] BDL advanced calibration: "
                    f"{bdl_saved} calibrations saved"
                )
        except Exception as e:
            logger.warning(f"[Analysis] BDL advanced calibration failed: {e}")

    return {
        "analysis": analysis.model_dump(),
        "calibrations_saved": calibrations_saved,
        "stats": stats,
    }
