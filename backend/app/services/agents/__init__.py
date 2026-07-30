"""AI agent registry — all 11 specialised agents.

Each agent wraps the central AIService with domain-specific prompts
and output parsing.  Agents are instantiated in ``routes.py`` and
injected into the services that need them.
"""

from app.services.agents.signal_analysis_agent import SignalAnalysisAgent
from app.services.agents.lineup_strategy_agent import LineupStrategyAgent
from app.services.agents.injury_impact_agent import InjuryImpactAgent
from app.services.agents.narrative_agent import NarrativeAgent
from app.services.agents.news_projection_agent import NewsProjectionAgent
from app.services.agents.coach_learning_agent import CoachLearningAgent
from app.services.agents.ownership_agent import OwnershipProjectionAgent
from app.services.agents.simulation_tuning_agent import SimulationTuningAgent
from app.services.agents.backtesting_agent import BacktestingAgent
from app.services.agents.expert_quality_agent import ExpertQualityAgent
from app.services.agents.chat_agent import ChatAgent

__all__ = [
    "SignalAnalysisAgent",
    "LineupStrategyAgent",
    "InjuryImpactAgent",
    "NarrativeAgent",
    "NewsProjectionAgent",
    "CoachLearningAgent",
    "OwnershipProjectionAgent",
    "SimulationTuningAgent",
    "BacktestingAgent",
    "ExpertQualityAgent",
    "ChatAgent",
]
