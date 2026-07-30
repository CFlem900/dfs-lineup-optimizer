"""Coach Profile Service — merges static profiles with DB-stored learned adjustments.

The static COACH_PROFILES dict in models/coach.py provides baseline coaching
tendencies for all 30 teams.  The CoachLearningAgent (Agent 6) periodically
analyzes recent game data and generates delta adjustments, which are persisted
to the coach_profile_update table.

This service merges both sources at runtime.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from app.models.coach import CoachProfile, get_coach_profile

logger = logging.getLogger(__name__)


class CoachProfileService:
    """Merges static coach profiles with learned DB adjustments."""

    def __init__(self):
        self._adjustments: Dict[int, dict] = {}  # team_id -> latest deltas

    async def load_learned_adjustments(self) -> int:
        """Load all active, non-expired coach profile updates from DB."""
        from app.db.database import is_db_available, get_session
        from app.db.models import CoachProfileUpdate
        from sqlalchemy import select, or_

        if not is_db_available():
            return 0

        try:
            now = datetime.now(timezone.utc)
            async with get_session() as session:
                stmt = select(CoachProfileUpdate).where(
                    CoachProfileUpdate.is_active == True,
                    or_(
                        CoachProfileUpdate.expires_at.is_(None),
                        CoachProfileUpdate.expires_at > now,
                    ),
                ).order_by(CoachProfileUpdate.created_at.desc())
                result = await session.execute(stmt)

                self._adjustments = {}
                for row in result.scalars().all():
                    # Only keep the latest per team
                    if row.team_id not in self._adjustments:
                        self._adjustments[row.team_id] = {
                            "starter_multiplier_adj": row.starter_multiplier_adj or 0,
                            "bench_multiplier_adj": row.bench_multiplier_adj or 0,
                            "star_multiplier_adj": row.star_multiplier_adj or 0,
                            "rotation_size_adj": row.rotation_size_adj or 0,
                            "b2b_penalty_adj": row.b2b_penalty_adj or 0,
                            "pace_factor_adj": row.pace_factor_adj or 0,
                            "confidence": row.confidence or 0.5,
                        }

            logger.info(
                f"[CoachProfile] Loaded adjustments for {len(self._adjustments)} teams"
            )
            return len(self._adjustments)
        except Exception as e:
            logger.warning(f"[CoachProfile] Failed to load: {e}")
            return 0

    def get_merged_profile(self, team_id: int) -> CoachProfile:
        """Return the static coach profile merged with any learned deltas."""
        profile = get_coach_profile(team_id)

        if team_id not in self._adjustments:
            return profile

        adj = self._adjustments[team_id]
        confidence = adj.get("confidence", 0.5)

        # Apply deltas scaled by confidence (higher confidence = stronger adjustment)
        merged = profile.model_copy()
        merged.starter_multiplier += adj["starter_multiplier_adj"] * confidence
        merged.bench_multiplier += adj["bench_multiplier_adj"] * confidence
        merged.star_multiplier += adj["star_multiplier_adj"] * confidence
        merged.min_rotation_size += adj["rotation_size_adj"]

        return merged

    async def persist_update(
        self, team_id: int, coach_name: str, deltas: dict,
        confidence: float = 0.5, reasoning: str = "",
        based_on_games: int = 0,
    ) -> None:
        """Persist a new coach profile update to the database."""
        from app.db.database import is_db_available, get_session
        from app.db.models import CoachProfileUpdate
        from datetime import timedelta

        if not is_db_available():
            return

        try:
            now = datetime.now(timezone.utc)
            async with get_session() as session:
                session.add(CoachProfileUpdate(
                    team_id=team_id,
                    coach_name=coach_name,
                    starter_multiplier_adj=deltas.get("starter_multiplier_adj", 0),
                    bench_multiplier_adj=deltas.get("bench_multiplier_adj", 0),
                    star_multiplier_adj=deltas.get("star_multiplier_adj", 0),
                    rotation_size_adj=deltas.get("rotation_size_adj", 0),
                    b2b_penalty_adj=deltas.get("b2b_penalty_adj", 0),
                    pace_factor_adj=deltas.get("pace_factor_adj", 0),
                    confidence=confidence,
                    reasoning=reasoning,
                    based_on_games=based_on_games,
                    expires_at=now + timedelta(days=14),
                ))
                await session.commit()
            logger.info(f"[CoachProfile] Persisted update for team {team_id}")
        except Exception as e:
            logger.warning(f"[CoachProfile] Failed to persist: {e}")
