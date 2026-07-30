"""Tournament import, analysis, and calibration endpoints."""

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File

from app.api.rate_limiter import limiter
from app.api.auth import require_admin_key
from app.api.dependencies import get_services
from app.config import get_settings as _get_settings
from app.db.database import is_db_available, get_session
from app.models.pagination import PaginationMeta, encode_cursor, decode_cursor
from app.models.responses import CalibrationHistoryResponse, CalibrationHistoryEntry

logger = logging.getLogger(__name__)
router = APIRouter()


def _compute_ownership_weight_adjustments(
    actual_ownership: Dict[str, float],
    contest_count: int,
) -> Dict[str, float]:
    """Derive ownership factor weight adjustments from actual contest data.

    Compares the distribution of actual ownership to expectations and
    generates calibration adjustments that the ownership model can use
    to improve future predictions.

    Returns dict of calibration_key → multiplier (centered on 1.0).
    """
    adjustments: Dict[str, float] = {}
    if not actual_ownership or contest_count < 3:
        return adjustments

    values = list(actual_ownership.values())
    if not values:
        return adjustments

    # Compute tier stats from actual ownership
    chalk = [v for v in values if v >= 20.0]    # High-owned
    mid = [v for v in values if 8.0 <= v < 20.0]
    low = [v for v in values if v < 8.0]

    avg_chalk = sum(chalk) / len(chalk) if chalk else 0
    avg_low = sum(low) / len(low) if low else 0

    # If chalk ownership is very concentrated (>30% avg), field is
    # heavily convergent → increase salary/star weights
    if len(chalk) >= 3 and avg_chalk > 30.0:
        adjustments["ownership_factor_salary_weight"] = 1.10
        adjustments["ownership_factor_star_premium_weight"] = 1.08

    # If low-owned players are plentiful and average is very low (<3%),
    # the field is ignoring contrarian plays → reduce value factor
    if len(low) >= 10 and avg_low < 3.0:
        adjustments["ownership_factor_value_weight"] = 0.95

    # If few players are chalk, field is playing more spread out →
    # reduce salary weight (it's over-predicting concentration)
    if len(chalk) <= 2 and len(values) >= 20:
        adjustments["ownership_factor_salary_weight"] = adjustments.get(
            "ownership_factor_salary_weight", 0.93
        )

    # Injury beneficiary: if mid-tier ownership is high, the field is
    # chasing injury news → boost injury factor
    if len(mid) >= 5 and (sum(mid) / len(mid)) > 14.0:
        adjustments["ownership_factor_injury_benefit_weight"] = 1.10

    return adjustments


@router.post("/tournament/import")
@limiter.limit("5/minute")
async def import_tournament_csv(
    request: Request,
    _auth=Depends(require_admin_key),
    file: UploadFile = File(...),
    contest_date: str = Query(..., description="Contest date YYYY-MM-DD"),
    contest_name: Optional[str] = Query(None, description="Contest name"),
    contest_type: str = Query("gpp", description="gpp, cash, or single_entry"),
):
    """Upload a DraftKings contest export CSV.

    Parses the CSV, stores contest metadata and individual lineup
    entries, and marks the top 1% as winners for analysis.
    """
    try:
        svc = get_services()

        # -- File type validation --
        _ALLOWED_MIME_TYPES = {"text/csv", "application/vnd.ms-excel", "application/octet-stream"}
        if file.content_type and file.content_type not in _ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid file type '{file.content_type}'. "
                       f"Only CSV files are accepted.",
            )
        if file.filename and not file.filename.lower().endswith(".csv"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid filename '{file.filename}'. "
                       f"Only .csv files are accepted.",
            )

        content = await file.read()
        _settings = _get_settings()
        max_bytes = _settings.max_upload_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File too large ({len(content)} bytes). "
                       f"Max allowed: {_settings.max_upload_size_mb} MB.",
            )
        csv_text = content.decode("utf-8")

        # -- CSV content sanitization & structure validation --
        csv_text = csv_text.replace("\x00", "")

        import csv as _csv
        import io as _io
        _reader = _csv.reader(_io.StringIO(csv_text))
        _header = next(_reader, None)
        if _header is None:
            raise HTTPException(
                status_code=400,
                detail="CSV file is empty -- no header row found.",
            )
        _REQUIRED_FIELDS = {"Rank", "EntryId", "EntryName"}
        _header_set = {h.strip() for h in _header}
        _missing = _REQUIRED_FIELDS - _header_set
        if _missing:
            raise HTTPException(
                status_code=400,
                detail=f"CSV missing required DraftKings columns: {', '.join(sorted(_missing))}. "
                       f"Found columns: {', '.join(_header_set)}",
            )

        result = await svc.tournament_import_service.import_csv(
            csv_content=csv_text,
            contest_date=contest_date,
            contest_name=contest_name,
            contest_type=contest_type,
        )
        result["message"] = (
            f"Imported {result['entries_imported']} entries, "
            f"top 1% = {result['top_1pct_count']} lineups"
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Tournament import failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tournament/import-batch")
@limiter.limit("3/minute")
async def import_tournament_batch(
    request: Request,
    _auth=Depends(require_admin_key),
    files: List[UploadFile] = File(...),
    contest_type: str = Query("gpp", description="gpp, cash, or single_entry"),
):
    """Upload multiple DraftKings contest export CSVs at once.

    Parses each CSV, auto-detects the contest date from the filename
    or CSV content, stores contest metadata and individual lineup
    entries, and marks the top 1% as winners for analysis.

    Filename convention: DKEntries_NBA_2026-03-27_Main.csv
    (date is extracted from YYYY-MM-DD pattern in filename)
    """
    import re as _re

    svc = get_services()
    _settings = _get_settings()
    max_bytes = _settings.max_upload_size_mb * 1024 * 1024

    results = []
    errors = []

    for file in files:
        fname = file.filename or "unknown.csv"
        try:
            # Validate file type
            if not fname.lower().endswith(".csv"):
                errors.append({"file": fname, "error": "Not a CSV file"})
                continue

            content = await file.read()
            if len(content) > max_bytes:
                errors.append({
                    "file": fname,
                    "error": f"Too large ({len(content)} bytes, max {_settings.max_upload_size_mb} MB)",
                })
                continue

            csv_text = content.decode("utf-8").replace("\x00", "")

            # Validate CSV structure
            import csv as _csv
            import io as _io
            _reader = _csv.reader(_io.StringIO(csv_text))
            _header = next(_reader, None)
            if _header is None:
                errors.append({"file": fname, "error": "Empty CSV"})
                continue
            _header_set = {h.strip() for h in _header}
            _missing = {"Rank", "EntryId", "EntryName"} - _header_set
            if _missing:
                errors.append({
                    "file": fname,
                    "error": f"Missing columns: {', '.join(sorted(_missing))}",
                })
                continue

            # Auto-detect date from filename (YYYY-MM-DD pattern)
            date_match = _re.search(r"(\d{4}-\d{2}-\d{2})", fname)
            if date_match:
                contest_date = date_match.group(1)
            else:
                # Fallback: use today's date
                from datetime import date as _date
                contest_date = _date.today().isoformat()

            # Extract contest name from filename
            # e.g. "DKEntries_NBA_2026-03-27_Main.csv" → "Main"
            contest_name = fname
            for remove in [".csv", ".CSV", contest_date, "DKEntries_", "DKEntries", "NBA_", "NBA"]:
                contest_name = contest_name.replace(remove, "")
            contest_name = contest_name.strip("_- ")
            if not contest_name:
                contest_name = fname

            result = await svc.tournament_import_service.import_csv(
                csv_content=csv_text,
                contest_date=contest_date,
                contest_name=contest_name,
                contest_type=contest_type,
            )
            result["file"] = fname
            result["contest_date"] = contest_date
            result["message"] = (
                f"Imported {result['entries_imported']} entries, "
                f"top 1% = {result['top_1pct_count']} lineups"
            )
            results.append(result)
            logger.info(
                "[TournamentBatch] %s → %d entries, date=%s",
                fname, result["entries_imported"], contest_date,
            )

        except Exception as e:
            errors.append({"file": fname, "error": str(e)})
            logger.error("[TournamentBatch] %s failed: %s", fname, e)

    total_entries = sum(r.get("entries_imported", 0) for r in results)
    return {
        "files_processed": len(results),
        "files_failed": len(errors),
        "total_entries_imported": total_entries,
        "results": results,
        "errors": errors,
    }


@router.get("/tournament/analysis")
@limiter.limit("5/minute")
async def run_tournament_analysis(request: Request, _auth=Depends(require_admin_key)):
    """Run AI tournament pattern analysis on stored contest results.

    Loads winning (top 1%) and field entries from the database, runs
    the TournamentAnalysisAgent, auto-saves calibration adjustments,
    and reloads the calibration cache.
    """
    if not is_db_available():
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        svc = get_services()
        from sqlalchemy import select
        from app.db.models import TournamentEntry, TournamentContest

        async with get_session() as session:
            winner_stmt = (
                select(TournamentEntry)
                .where(TournamentEntry.is_winner == 1)
                .order_by(TournamentEntry.rank)
                .limit(100)
            )
            winner_result = await session.execute(winner_stmt)
            winners = winner_result.scalars().all()

            field_stmt = (
                select(TournamentEntry)
                .where(TournamentEntry.is_winner == 0)
                .order_by(TournamentEntry.rank)
                .limit(50)
            )
            field_result = await session.execute(field_stmt)
            field = field_result.scalars().all()

            contest_stmt = select(TournamentContest)
            contest_result = await session.execute(contest_stmt)
            contests = contest_result.scalars().all()

        if not winners:
            return {
                "analysis": None,
                "message": "No tournament data available. Import contest CSVs first.",
            }

        # Filter to entries that actually have lineup data
        winners_with_data = [
            w for w in winners if w.lineup_data and len(w.lineup_data) > 0
        ]
        field_with_data = [
            f for f in field if f.lineup_data and len(f.lineup_data) > 0
        ]

        if not winners_with_data:
            return {
                "analysis": None,
                "message": (
                    f"Found {len(winners)} winner entries but none have "
                    "lineup data. Try re-importing the CSV -- the parser "
                    "may not have matched the format."
                ),
            }

        winning_data = [
            {
                "rank": w.rank,
                "points": w.points,
                "lineup_data": w.lineup_data,
                "total_salary": w.total_salary,
            }
            for w in winners_with_data
        ]
        field_data = [
            {
                "rank": f.rank,
                "points": f.points,
                "lineup_data": f.lineup_data,
                "total_salary": f.total_salary,
            }
            for f in field_with_data
        ]

        # Build optional player-team mapping for stacking detection.
        # Uses the cached player pool if available; degrades gracefully.
        _player_team_map = None
        try:
            from app.services.agents.tournament_analysis_agent import (
                TournamentAnalysisAgent,
            )
            _nba_cache = getattr(svc, "nba_data_cache_service", None)
            if _nba_cache:
                _cached_pool = getattr(_nba_cache, "get_player_pool", lambda: None)()
                if _cached_pool:
                    _player_team_map = TournamentAnalysisAgent.build_player_team_map(
                        _cached_pool,
                    )
                    logger.debug(
                        f"[Tournament] Built player-team map with "
                        f"{len(_player_team_map)} entries for stacking detection"
                    )
        except Exception:
            pass  # Stacking detection will be unavailable

        analysis = svc.tournament_analysis_agent.analyze_tournament_results(
            winning_entries=winning_data,
            field_entries=field_data,
            contest_metadata={
                "contest_count": len(contests),
                "date_range": "all imported",
            },
            player_team_map=_player_team_map,
        )

        if analysis:
            # Normalize AI-generated keys -> keys the CalibrationService reads.
            # The AI may invent non-standard names; this mapping catches them.
            _KEY_REMAP = {
                # Ownership variants
                "ownership_contrarian_weight": "ownership_threshold_adj",
                "ownership_fade_multiplier": "ownership_threshold_adj",
                "contrarian_selection_bias": "ownership_threshold_adj",
                # Salary tier variants
                "salary_tier_value_boost": "salary_tier_value",
                "salary_tier_value_C": "salary_tier_value",
                "value_tier_boost": "salary_tier_value",
                "salary_tier_high_boost": "salary_tier_high",
                "salary_tier_mid_boost": "salary_tier_mid",
                # Stacking variants
                "stacking_reduce_weight": "stacking_2man_weight",
                "stacking_reduction": "stacking_2man_weight",
                # Bring-back variants
                "bringback_weight": "stacking_bringback_weight",
                "bring_back_weight": "stacking_bringback_weight",
                "stacking_bringback": "stacking_bringback_weight",
                # Guard flexibility -> PG bias
                "guard_flexibility_weight": "position_PG_bias",
                # Common misspellings / alternatives
                "position_PG_boost": "position_PG_bias",
                "position_SG_boost": "position_SG_bias",
                "position_SF_boost": "position_SF_bias",
                "position_PF_boost": "position_PF_bias",
                "position_C_boost": "position_C_bias",
            }

            # Valid keys the CalibrationService actually reads
            _VALID_KEYS = {
                "position_PG_bias", "position_SG_bias", "position_SF_bias",
                "position_PF_bias", "position_C_bias",
                "salary_tier_high", "salary_tier_mid", "salary_tier_value",
                "stacking_2man_weight", "stacking_3man_weight",
                "stacking_bringback_weight",
                "ownership_threshold_adj",
                "game_context_high_total", "game_context_b2b",
                "game_context_blowout",
                # Ownership model factor weights
                "ownership_factor_value_weight",
                "ownership_factor_salary_weight",
                "ownership_factor_game_env_weight",
                "ownership_factor_expert_weight",
                "ownership_factor_projection_weight",
                "ownership_factor_star_premium_weight",
                "ownership_factor_scarcity_weight",
                "ownership_factor_injury_benefit_weight",
                # GPP constraint overrides (from Agent 9 post-mortem)
                "gpp_ownership_cap", "gpp_pivot_threshold",
                "gpp_pivot_min_count", "gpp_ceiling_weight",
                "gpp_bringback_salary_threshold", "gpp_salary_floor_pct",
            }

            raw_adjustments = analysis.calibration_adjustments or {}
            normalized: Dict[str, float] = {}
            for key, value in raw_adjustments.items():
                mapped = _KEY_REMAP.get(key, key)
                if mapped in _VALID_KEYS:
                    # If multiple AI keys map to the same target, average them
                    if mapped in normalized:
                        normalized[mapped] = (normalized[mapped] + value) / 2.0
                    else:
                        normalized[mapped] = value
                else:
                    logger.warning(
                        f"[Tournament] Dropping unknown calibration key: "
                        f"{key} (mapped={mapped}, value={value})"
                    )

            if normalized:
                logger.info(
                    f"[Tournament] Normalized {len(raw_adjustments)} AI keys "
                    f"-> {len(normalized)} valid calibrations"
                )

            # Build category map from patterns
            category_map = {}
            for pattern in analysis.patterns:
                raw_key = pattern.pattern_key
                mapped_key = _KEY_REMAP.get(raw_key, raw_key)
                if mapped_key in normalized:
                    category_map[mapped_key] = pattern.category

            # Normalize per-adjustment reasoning using the same key remap
            raw_reasoning = analysis.per_adjustment_reasoning or {}
            normalized_reasoning: Dict[str, str] = {}
            for key, reason_text in raw_reasoning.items():
                mapped = _KEY_REMAP.get(key, key)
                if mapped in normalized:
                    normalized_reasoning[mapped] = reason_text

            await svc.calibration_service.save_tournament_calibrations(
                adjustments=normalized,
                category_map=category_map,
                metadata={
                    "contest_count": len(contests),
                    "entry_count": len(winners),
                    "confidence": 0.7,
                    "reasoning": analysis.reasoning,
                },
                reasoning_map=normalized_reasoning or None,
            )
            # Reload calibrations into memory
            await svc.calibration_service.load_calibrations()

            # ── Ownership learning: compare projected vs actual ─────────
            # When enough contest data exists, compute ownership prediction
            # errors and derive factor weight adjustments.
            try:
                from app.config.constants import OWNERSHIP_LEARNING_MIN_CONTESTS
                if len(contests) >= OWNERSHIP_LEARNING_MIN_CONTESTS:
                    latest_contest = contests[-1]
                    actual_own = svc.tournament_import_service.compute_actual_ownership(
                        latest_contest.id
                    )
                    if actual_own and len(actual_own) >= 10:
                        ownership_adjustments = _compute_ownership_weight_adjustments(
                            actual_own, len(contests),
                        )
                        if ownership_adjustments:
                            await svc.calibration_service.save_tournament_calibrations(
                                adjustments=ownership_adjustments,
                                category_map={
                                    k: "ownership_model"
                                    for k in ownership_adjustments
                                },
                                metadata={
                                    "contest_count": len(contests),
                                    "entry_count": len(winners),
                                    "confidence": 0.6,
                                    "reasoning": (
                                        "Ownership model learning: auto-derived "
                                        f"from {len(actual_own)} players across "
                                        f"{len(contests)} contests"
                                    ),
                                },
                            )
                            await svc.calibration_service.load_calibrations()
                            logger.info(
                                f"[OwnershipLearning] Saved {len(ownership_adjustments)} "
                                "weight adjustments"
                            )
            except Exception as e:
                logger.warning(f"Ownership learning failed (non-fatal): {e}")

            return {"analysis": analysis.model_dump()}

        return {
            "analysis": None,
            "message": (
                "AI analysis returned no results. "
                f"({len(winning_data)} winners, {len(field_data)} field entries sent)"
            ),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Tournament analysis failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tournament/gpp-postmortem")
@limiter.limit("5/minute")
async def run_gpp_postmortem(
    request: Request,
    _auth=Depends(require_admin_key),
    top_n: int = Query(10, ge=1, le=50, description="Top N finishers per contest"),
    contest_type: str = Query("gpp", description="Contest type filter"),
):
    """Run GPP post-mortem analysis on stored contest top finishers.

    Loads the top N finishers from all GPP contests in the database,
    runs Agent 9's GPP blueprint analysis, saves constraint overrides
    to the calibration store, and returns the full blueprint.
    """
    if not is_db_available():
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        svc = get_services()
        from sqlalchemy import select
        from app.db.models import TournamentEntry, TournamentContest

        # Load GPP contests
        async with get_session() as session:
            contest_stmt = (
                select(TournamentContest)
                .where(TournamentContest.contest_type == contest_type)
            )
            contest_result = await session.execute(contest_stmt)
            contests = contest_result.scalars().all()

            if not contests:
                return {
                    "blueprint": None,
                    "message": f"No {contest_type} contests found. Import contest CSVs first.",
                }

            contest_ids = [c.id for c in contests]

            # Load top N entries per contest
            top_entries_stmt = (
                select(TournamentEntry)
                .where(
                    TournamentEntry.contest_id_fk.in_(contest_ids),
                    TournamentEntry.rank <= top_n,
                )
                .order_by(TournamentEntry.rank)
            )
            top_result = await session.execute(top_entries_stmt)
            top_entries_raw = top_result.scalars().all()

        if not top_entries_raw:
            return {
                "blueprint": None,
                "message": f"No entries found with rank <= {top_n}.",
            }

        # Filter to entries with actual lineup data
        top_entries = [
            {
                "rank": e.rank,
                "points": e.points,
                "lineup_data": e.lineup_data,
                "total_salary": e.total_salary,
            }
            for e in top_entries_raw
            if e.lineup_data and len(e.lineup_data) > 0
        ]

        if not top_entries:
            return {
                "blueprint": None,
                "message": f"Found {len(top_entries_raw)} entries but none have lineup data.",
            }

        # Build player-team map for stacking detection (reuse Agent 12 helper)
        _player_team_map = None
        try:
            if hasattr(svc, "tournament_analysis_agent") and svc.tournament_analysis_agent:
                all_lineups = [e["lineup_data"] for e in top_entries]
                _player_team_map = svc.tournament_analysis_agent.build_player_team_map(
                    all_lineups,
                )
        except Exception:
            pass

        # Run Agent 9 GPP post-mortem (or deterministic fallback)
        if not svc.backtesting_agent:
            from app.services.agents.backtesting_agent import (
                compute_gpp_blueprint,
                compute_deterministic_gpp_constraints,
            )
            stats = compute_gpp_blueprint(top_entries, _player_team_map)
            overrides = compute_deterministic_gpp_constraints(stats)
            blueprint_data = {
                "contest_count": len(contests),
                "top_n_analyzed": top_n,
                "observed_stats": stats,
                "constraint_overrides": overrides,
                "reasoning": "Deterministic analysis (Agent 9 unavailable)",
            }
        else:
            blueprint = svc.backtesting_agent.analyze_gpp_postmortem(
                top_entries=top_entries,
                contest_metadata={
                    "contest_count": len(contests),
                    "top_n": top_n,
                    "date_range": "all imported GPP contests",
                },
                player_team_map=_player_team_map,
            )
            if not blueprint:
                return {
                    "blueprint": None,
                    "message": "GPP post-mortem analysis returned no results.",
                }
            blueprint_data = blueprint.model_dump()

        # Save constraint overrides to calibration store
        overrides = blueprint_data.get("constraint_overrides") or {}
        if overrides and svc.calibration_service:
            reasoning_map = {}
            for rec in blueprint_data.get("recommended_constraints", []):
                if isinstance(rec, dict):
                    reasoning_map[rec.get("constraint_key", "")] = rec.get("reasoning", "")

            await svc.calibration_service.save_gpp_blueprint_calibrations(
                constraint_overrides=overrides,
                metadata={
                    "contest_count": len(contests),
                    "entry_count": len(top_entries),
                    "confidence": 0.7,
                    "reasoning": blueprint_data.get("reasoning", ""),
                },
                reasoning_map=reasoning_map or None,
            )
            await svc.calibration_service.load_calibrations()

        return {"blueprint": blueprint_data}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"GPP post-mortem failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tournament/calibrations")
async def get_tournament_calibrations(_auth=Depends(require_admin_key)):
    """View all active calibration adjustments.

    Returns both tournament-learned and backtest-learned calibrations
    with their metadata (category, confidence, reasoning).
    """
    try:
        svc = get_services()
        merged = await svc.calibration_service.load_calibrations()

        entries = []
        if is_db_available():
            from sqlalchemy import select
            from app.db.models import TournamentCalibration

            async with get_session() as session:
                stmt = select(TournamentCalibration).order_by(
                    TournamentCalibration.category
                )
                result = await session.execute(stmt)
                for row in result.scalars().all():
                    entries.append({
                        "calibration_key": row.calibration_key,
                        "category": row.category,
                        "adjustment_value": row.adjustment_value,
                        "raw_adjustment": row.raw_adjustment or row.adjustment_value,
                        "confidence": row.confidence or 0.5,
                        "based_on_contests": row.based_on_contests or 0,
                        "source": row.source or "tournament",
                        "reasoning": row.reasoning,
                    })

        return {
            "calibrations": entries,
            "total_count": len(merged),
            "merged_calibrations": merged,
        }
    except Exception as e:
        logger.error(f"Calibration fetch failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tournament/calibrations/reset")
async def reset_tournament_calibrations(
    _auth=Depends(require_admin_key),
    source: Optional[str] = Query(
        None, description="'tournament', 'backtest', or None for all"
    ),
):
    """Clear learned calibrations.

    Optionally filter by source to only clear tournament-learned or
    backtest-learned adjustments.
    """
    try:
        svc = get_services()
        count = await svc.calibration_service.reset_calibrations(source=source)
        return {"cleared": count, "source": source or "all"}
    except Exception as e:
        logger.error(f"Calibration reset failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tournament/calibrations/{key}/rollback")
async def rollback_calibration(key: str, _auth=Depends(require_admin_key)):
    """Deactivate a specific calibration key (set is_active=False)."""
    try:
        svc = get_services()
        found = await svc.calibration_service.rollback_calibration(key)
        if not found:
            raise HTTPException(
                status_code=404,
                detail=f"Calibration key '{key}' not found",
            )
        return {"rolled_back": key, "success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Calibration rollback failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tournament/calibrations/history", response_model=CalibrationHistoryResponse)
async def get_calibration_history(
    _auth=Depends(require_admin_key),
    key: Optional[str] = Query(
        None, description="Filter by calibration_key"
    ),
    limit: int = Query(50, ge=1, le=500, description="Max rows to return"),
    cursor: Optional[str] = Query(None, description="Opaque pagination cursor"),
):
    """View full calibration history (active + inactive) for audit.

    Supports cursor-based pagination via ``limit`` and ``cursor`` params.
    """
    try:
        svc = get_services()
        history = await svc.calibration_service.get_calibration_history(key)

        # Cursor is an offset index into the in-memory list
        offset = 0
        if cursor:
            try:
                cursor_data = decode_cursor(cursor)
                offset = int(cursor_data["offset"])
            except (KeyError, ValueError) as exc:
                raise HTTPException(
                    status_code=400, detail=f"Invalid cursor: {exc}"
                )

        # Slice with limit + 1 to detect more pages
        page = history[offset : offset + limit + 1]
        has_more = len(page) > limit
        page_rows = page[:limit] if has_more else page

        next_cursor = None
        if has_more:
            next_cursor = encode_cursor(offset=offset + limit)

        return CalibrationHistoryResponse(
            history=[CalibrationHistoryEntry(**entry) for entry in page_rows],
            count=len(history),
            pagination=PaginationMeta(
                limit=limit,
                has_more=has_more,
                next_cursor=next_cursor,
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Calibration history failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tournament/ownership-accuracy")
async def get_ownership_accuracy(
    _auth=Depends(require_admin_key),
    contest_id: int = Query(..., description="Contest ID to analyse"),
):
    """Compare projected ownership vs actual field ownership for a contest.

    Derives actual ownership from imported entries (player frequency),
    then compares against our projected ownership if available in the
    player pool cache.

    Returns MAE, RMSE, Pearson correlation, worst misses, and tier splits.
    """
    try:
        svc = get_services()
        actual = await svc.tournament_import_service.compute_actual_ownership(contest_id)
        if not actual:
            raise HTTPException(
                status_code=404,
                detail=f"No entries found for contest_id={contest_id}",
            )
        return {
            "contest_id": contest_id,
            "player_count": len(actual),
            "actual_ownership": actual,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ownership accuracy failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
