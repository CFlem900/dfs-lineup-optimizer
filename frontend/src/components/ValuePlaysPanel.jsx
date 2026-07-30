import React, { useState, useMemo } from 'react'
import {
  Gem,
  TrendingUp,
  Zap,
  DollarSign,
} from 'lucide-react'

/**
 * Value scoring algorithm — TWO MODES:
 *
 * ═══════════════════════════════════════════════════════
 * SALARY-AWARE MODE  (when DK salary data is available)
 * ═══════════════════════════════════════════════════════
 *
 * 1. SALARY VALUE (35% weight)
 *    FP per $1K of salary — the core DFS value metric.
 *    A $4,500 player projecting 25 FP = 5.56x (elite).
 *    A $9,000 player projecting 35 FP = 3.89x (below avg).
 *
 * 2. CEILING UPSIDE/$ (20% weight)
 *    Ceiling per $1K — tournament upside relative to cost.
 *
 * 3. GAME ENVIRONMENT (20% weight)
 *    Pace + total of the matchup. Fast, high-scoring games
 *    create more possessions = more DFS opportunity.
 *
 * 4. FLOOR SAFETY/$ (10% weight)
 *    Floor per $1K — cash-game safety relative to cost.
 *
 * 5. MINUTES CONFIDENCE (10% weight)
 *    adjusted / baseline minutes ratio — injury replacements
 *    getting a boost are undervalued by the market.
 *
 * 6. USAGE EFFICIENCY (5% weight)
 *    Fantasy points per minute — efficient producers.
 *
 * ═══════════════════════════════════════════════════════
 * LEGACY MODE  (when salary data is NOT available)
 * ═══════════════════════════════════════════════════════
 *
 * Falls back to the projection-only algorithm:
 *   Ceiling Upside 30%, Minutes 20%, Game Env 25%,
 *   Floor Safety 15%, Usage Efficiency 10%
 *
 * Value tiers:
 *   💎 SMASH   = score >= 80  (top-tier value)
 *   🔥 STRONG  = score >= 65  (solid value play)
 *   ⚡ UPSIDE  = score >= 50  (worthwhile ceiling play)
 */

const VALUE_TIERS = [
  { min: 80, label: 'SMASH', color: 'text-yellow-300', bg: 'bg-yellow-500/10 border-yellow-500/30', icon: Gem },
  { min: 65, label: 'STRONG', color: 'text-ticker-green', bg: 'bg-ticker-green/10 border-ticker-green/30', icon: TrendingUp },
  { min: 50, label: 'UPSIDE', color: 'text-cyan-400', bg: 'bg-cyan-500/10 border-cyan-500/30', icon: Zap },
]

// ---------------------------------------------------------------------------
// Build candidates from player data
// ---------------------------------------------------------------------------

function buildCandidates(playersData, games, platform) {
  if (!playersData || playersData.length === 0 || !games || games.length === 0) {
    return []
  }

  const gameByTeam = {}
  for (const g of games) {
    gameByTeam[g.away_team.team_id] = g
    gameByTeam[g.home_team.team_id] = g
  }

  const teamIdByAbbr = {}
  for (const g of games) {
    teamIdByAbbr[g.away_team.team_abbreviation] = g.away_team.team_id
    teamIdByAbbr[g.home_team.team_abbreviation] = g.home_team.team_id
  }

  const candidates = []
  for (const team of playersData) {
    if (!team.rotation || !team.rotation.projections) continue
    const teamId = teamIdByAbbr[team.teamAbbr] || team.teamId
    const game = gameByTeam[teamId]

    for (const p of team.rotation.projections) {
      if (p.adjusted_minutes <= 0) continue

      const fp = platform === 'dk' ? p.dk_points : p.fd_points
      const floor = platform === 'dk' ? p.dk_floor : p.fd_floor
      const ceiling = platform === 'dk' ? p.dk_ceiling : p.fd_ceiling

      if (!fp || fp <= 0) continue

      candidates.push({
        ...p,
        teamAbbr: team.teamAbbr,
        fp,
        floor: floor || 0,
        ceiling: ceiling || 0,
        salary: p.dk_salary || null,
        game,
      })
    }
  }

  return candidates
}

// ---------------------------------------------------------------------------
// Game environment helpers (shared by both modes)
// ---------------------------------------------------------------------------

function computeEnvMetrics(games) {
  const paces = games.map((g) => g.projected_pace)
  const totals = games.map((g) => g.projected_total)
  return {
    minPace: Math.min(...paces),
    maxPace: Math.max(...paces),
    minTotal: Math.min(...totals),
    maxTotal: Math.max(...totals),
  }
}

function computeEnvScore(game, env) {
  if (!game) return 0.5
  const paceNorm =
    env.maxPace > env.minPace
      ? (game.projected_pace - env.minPace) / (env.maxPace - env.minPace)
      : 0.5
  const totalNorm =
    env.maxTotal > env.minTotal
      ? (game.projected_total - env.minTotal) / (env.maxTotal - env.minTotal)
      : 0.5
  return paceNorm * 0.4 + totalNorm * 0.6
}

// ---------------------------------------------------------------------------
// SALARY-AWARE scoring (primary mode when salary data available)
// ---------------------------------------------------------------------------

function computeSalaryAwareScores(candidates, games) {
  const withSalary = candidates.filter((c) => c.salary && c.salary > 0)
  if (withSalary.length === 0) return []

  const env = computeEnvMetrics(games)

  // Compute per-$1K metrics
  const values = withSalary.map((c) => (c.fp / c.salary) * 1000)
  const ceilingValues = withSalary.map((c) => (c.ceiling / c.salary) * 1000)
  const floorValues = withSalary.map((c) => (c.floor / c.salary) * 1000)
  const fpPerMin = withSalary.map((c) => c.fp / Math.max(c.adjusted_minutes, 1))

  // Min-max normalization bounds
  const maxValue = Math.max(...values)
  const minValue = Math.min(...values)
  const maxCeilVal = Math.max(...ceilingValues)
  const minCeilVal = Math.min(...ceilingValues)
  const maxFloorVal = Math.max(...floorValues)
  const minFloorVal = Math.min(...floorValues)
  const maxFPPerMin = Math.max(...fpPerMin)

  const scored = withSalary.map((c, i) => {
    // 1. Salary Value (35%)
    const salaryScore =
      maxValue > minValue
        ? (values[i] - minValue) / (maxValue - minValue)
        : 0.5

    // 2. Ceiling Upside/$ (20%)
    const ceilingScore =
      maxCeilVal > minCeilVal
        ? (ceilingValues[i] - minCeilVal) / (maxCeilVal - minCeilVal)
        : 0.5

    // 3. Game Environment (20%)
    const envScore = computeEnvScore(c.game, env)

    // 4. Floor Safety/$ (10%)
    const floorScore =
      maxFloorVal > minFloorVal
        ? (floorValues[i] - minFloorVal) / (maxFloorVal - minFloorVal)
        : 0.5

    // 5. Minutes Confidence (10%)
    const minutesBoost = c.adjusted_minutes / Math.max(c.baseline_minutes, 1)
    const minutesScore = Math.min(Math.max((minutesBoost - 0.9) / 0.3, 0), 1.0)

    // 6. Usage Efficiency (5%)
    const effScore = maxFPPerMin > 0 ? fpPerMin[i] / maxFPPerMin : 0

    // Composite
    const raw =
      salaryScore * 0.35 +
      ceilingScore * 0.20 +
      envScore * 0.20 +
      floorScore * 0.10 +
      minutesScore * 0.10 +
      effScore * 0.05

    const score = Math.round(raw * 100)

    // Value per $1K
    const valuePerK = (c.fp / c.salary) * 1000

    // Build reasons
    const reasons = []
    if (valuePerK >= 5.0) reasons.push(`${valuePerK.toFixed(1)}x Value`)
    else if (valuePerK >= 4.0) reasons.push(`${valuePerK.toFixed(1)}x Value`)
    if (minutesBoost >= 1.05) reasons.push('Minutes \u2191')
    if (c.game && c.game.pace_label === 'Fast') reasons.push('Fast Pace')
    if (c.game && c.game.projected_total >= 230) reasons.push('High Total')
    if (c.salary <= 5000) reasons.push('Cheap')
    if (ceilingValues[i] >= 6.0) reasons.push('Big Ceiling/$')

    return {
      ...c,
      valueScore: score,
      valuePerK,
      reasons,
    }
  })

  return scored
    .filter((s) => s.valueScore >= 50)
    .sort((a, b) => b.valueScore - a.valueScore)
}

// ---------------------------------------------------------------------------
// LEGACY scoring (fallback when no salary data)
// ---------------------------------------------------------------------------

function computeLegacyScores(candidates, games) {
  if (candidates.length === 0) return []

  const env = computeEnvMetrics(games)
  const fpPerMin = candidates.map((c) => c.fp / Math.max(c.adjusted_minutes, 1))
  const maxFPPerMin = Math.max(...fpPerMin)

  const scored = candidates.map((c, i) => {
    // 1. Ceiling Upside (30%)
    const ceilingRatio = c.ceiling / c.fp
    const ceilingScore = Math.min(Math.max((ceilingRatio - 1.0) / 0.3, 0), 1.0)

    // 2. Minutes Confidence (20%)
    const minutesBoost = c.adjusted_minutes / Math.max(c.baseline_minutes, 1)
    const minutesScore = Math.min(Math.max((minutesBoost - 0.9) / 0.3, 0), 1.0)

    // 3. Game Environment (25%)
    const envScore = computeEnvScore(c.game, env)

    // 4. Floor Safety (15%)
    const floorRatio = c.floor / c.fp
    const floorScore = Math.min(floorRatio / 0.9, 1.0)

    // 5. Usage Efficiency (10%)
    const effScore = maxFPPerMin > 0 ? fpPerMin[i] / maxFPPerMin : 0

    const raw =
      ceilingScore * 0.30 +
      minutesScore * 0.20 +
      envScore * 0.25 +
      floorScore * 0.15 +
      effScore * 0.10

    const score = Math.round(raw * 100)

    const reasons = []
    if (minutesBoost >= 1.05) reasons.push('Minutes \u2191')
    if (c.game && c.game.pace_label === 'Fast') reasons.push('Fast Pace')
    if (c.game && c.game.projected_total >= 230) reasons.push('High Total')
    if (ceilingRatio >= 1.25) reasons.push('Big Ceiling')
    if (floorRatio >= 0.85) reasons.push('Safe Floor')

    return {
      ...c,
      valueScore: score,
      valuePerK: null,
      reasons,
    }
  })

  return scored
    .filter((s) => s.valueScore >= 50)
    .sort((a, b) => b.valueScore - a.valueScore)
}

// ---------------------------------------------------------------------------
// Main entry — auto-detects salary availability
// ---------------------------------------------------------------------------

function computeValuePlays(playersData, games, platform) {
  const candidates = buildCandidates(playersData, games, platform)
  if (candidates.length === 0) return []

  // Check if we have salary data for a meaningful portion of players
  const hasSalaryData = candidates.some((c) => c.salary != null && c.salary > 0)

  if (hasSalaryData) {
    return computeSalaryAwareScores(candidates, games)
  }
  return computeLegacyScores(candidates, games)
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

function ValuePlaysPanel({ playersData, playersLoading, games }) {
  const [platform, setPlatform] = useState('dk')

  const valuePlays = useMemo(
    () => computeValuePlays(playersData, games, platform),
    [playersData, games, platform]
  )

  // Detect if we're in salary mode
  const hasSalary = valuePlays.some((p) => p.valuePerK != null)

  const getTier = (score) => {
    for (const tier of VALUE_TIERS) {
      if (score >= tier.min) return tier
    }
    return null
  }

  return (
    <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden flex flex-col">
      {/* Header */}
      <div className="px-4 py-3 border-b border-ticker-border flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-semibold uppercase tracking-wider">
            Value Plays
          </h2>
          {!playersLoading && valuePlays.length > 0 && (
            <span className="px-2 py-0.5 bg-yellow-500/10 text-yellow-300 text-xs font-bold rounded-full">
              {valuePlays.length}
            </span>
          )}
          {hasSalary && (
            <span className="px-1.5 py-0.5 bg-green-900/30 text-green-400 text-[9px] font-bold rounded border border-green-500/20">
              <DollarSign className="w-2.5 h-2.5 inline -mt-0.5" />
              SAL
            </span>
          )}
        </div>
        <div className="flex items-center bg-ticker-bg rounded-md p-0.5">
          <button
            onClick={() => setPlatform('dk')}
            className={`px-2.5 py-1 text-xs font-semibold rounded transition-colors ${
              platform === 'dk'
                ? 'bg-green-600 text-white'
                : 'text-ticker-muted hover:text-white'
            }`}
          >
            DK
          </button>
          <button
            onClick={() => setPlatform('fd')}
            className={`px-2.5 py-1 text-xs font-semibold rounded transition-colors ${
              platform === 'fd'
                ? 'bg-blue-600 text-white'
                : 'text-ticker-muted hover:text-white'
            }`}
          >
            FD
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto max-h-[70vh]">
        {playersLoading && (
          <div className="py-12 flex flex-col items-center gap-3">
            <Gem className="w-6 h-6 text-yellow-300 animate-pulse" />
            <p className="text-sm text-ticker-muted">
              Analyzing value...
            </p>
          </div>
        )}

        {!playersLoading && valuePlays.length === 0 && (
          <div className="py-12 flex flex-col items-center gap-3">
            <Gem className="w-8 h-8 text-ticker-border" />
            <p className="text-sm text-ticker-muted">
              No value plays identified
            </p>
            <p className="text-xs text-ticker-muted/60 max-w-[200px] text-center">
              Value plays appear when player projections load
            </p>
          </div>
        )}

        {!playersLoading && valuePlays.length > 0 && (
          <div className="divide-y divide-ticker-border/20">
            {valuePlays.map((player) => {
              const tier = getTier(player.valueScore)
              if (!tier) return null
              const TierIcon = tier.icon
              const stats = player.projected_stats || {}
              const matchup = player.game
                ? `${player.game.away_team.team_abbreviation}@${player.game.home_team.team_abbreviation}`
                : ''

              return (
                <div
                  key={`${player.teamAbbr}-${player.player_id}`}
                  className="px-3 py-2.5 hover:bg-ticker-bg/30 transition-colors"
                >
                  {/* Row 1: Name + Score */}
                  <div className="flex items-center justify-between mb-1">
                    <div className="flex items-center gap-2 min-w-0">
                      <TierIcon className={`w-3.5 h-3.5 flex-shrink-0 ${tier.color}`} />
                      <span className="text-xs font-semibold truncate">
                        {player.player_name}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5 flex-shrink-0 ml-2">
                      <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded border ${tier.bg}`}>
                        <span className={tier.color}>{tier.label}</span>
                      </span>
                      <span className="text-[10px] text-ticker-muted tabular-nums font-bold">
                        {player.valueScore}
                      </span>
                    </div>
                  </div>

                  {/* Row 2: Team + Pos + Salary + Matchup + FP + Value */}
                  <div className="flex items-center justify-between mb-1.5">
                    <div className="flex items-center gap-1.5 text-[10px] text-ticker-muted">
                      <span className="px-1 py-0.5 bg-ticker-bg rounded font-semibold">
                        {player.teamAbbr}
                      </span>
                      <span className="px-1 py-0.5 bg-ticker-bg rounded">
                        {player.position}
                      </span>
                      {player.salary && (
                        <span className="px-1 py-0.5 bg-green-900/30 rounded text-green-400 font-semibold">
                          ${(player.salary / 1000).toFixed(1)}K
                        </span>
                      )}
                      {matchup && (
                        <span className="text-ticker-muted/60">
                          {matchup}
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      {player.valuePerK != null && (
                        <span
                          className={`text-[10px] font-bold tabular-nums ${
                            player.valuePerK >= 5.0
                              ? 'text-yellow-300'
                              : player.valuePerK >= 4.0
                                ? 'text-green-400'
                                : 'text-ticker-muted'
                          }`}
                        >
                          {player.valuePerK.toFixed(1)}x
                        </span>
                      )}
                      <span
                        className={`text-xs font-bold tabular-nums ${
                          platform === 'dk' ? 'text-green-400' : 'text-blue-400'
                        }`}
                      >
                        {player.fp.toFixed(1)}
                      </span>
                    </div>
                  </div>

                  {/* Row 3: Stats line */}
                  <div className="flex items-center gap-3 text-[10px] tabular-nums text-ticker-muted mb-1.5">
                    <span>{player.adjusted_minutes.toFixed(0)}m</span>
                    <span>{(stats.pts || 0).toFixed(1)}p</span>
                    <span>{(stats.reb || 0).toFixed(1)}r</span>
                    <span>{(stats.ast || 0).toFixed(1)}a</span>
                    <span className="ml-auto text-ticker-muted/60">
                      {player.floor.toFixed(0)}&ndash;{player.ceiling.toFixed(0)}
                    </span>
                  </div>

                  {/* Row 4: Reason tags */}
                  {player.reasons.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {player.reasons.map((reason) => (
                        <span
                          key={reason}
                          className="text-[9px] px-1.5 py-0.5 rounded bg-ticker-bg text-ticker-muted/80"
                        >
                          {reason}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Footer — tier legend */}
      {!playersLoading && valuePlays.length > 0 && (
        <div className="px-3 py-2 border-t border-ticker-border flex items-center justify-between text-[10px] text-ticker-muted flex-shrink-0">
          <div className="flex items-center gap-3">
            {VALUE_TIERS.map((tier) => {
              const TierIcon = tier.icon
              return (
                <div key={tier.label} className="flex items-center gap-1">
                  <TierIcon className={`w-3 h-3 ${tier.color}`} />
                  <span className={tier.color}>{tier.label}</span>
                </div>
              )
            })}
          </div>
          <span className="text-ticker-muted/60">
            {hasSalary ? 'Salary-Aware' : 'Projection Only'}
          </span>
        </div>
      )}
    </div>
  )
}

export default ValuePlaysPanel
