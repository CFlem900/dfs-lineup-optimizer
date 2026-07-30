/**
 * LineupDisplay — Renders the filled roster slots for an optimized DFS lineup.
 *
 * Shows 8 slots (DK) or 9 slots (FD), each with player name, team, position,
 * salary, and projected FP.  A salary progress bar and summary footer show
 * the lineup totals.
 */

import React from 'react'
import { User, DollarSign, Target, TrendingUp, TrendingDown } from 'lucide-react'
import { getDkRosterSlots } from '../config/sportShapes'

// FD slot display order. DK is sport-dependent — see getDkRosterSlots(sport).
const FD_DISPLAY = ['PG', 'PG', 'SG', 'SG', 'SF', 'SF', 'PF', 'PF', 'C']

const GRADE_COLORS = {
  'A+': 'bg-green-600 text-white',
  A: 'bg-green-600/80 text-white',
  'B+': 'bg-yellow-500 text-black',
  B: 'bg-yellow-500/80 text-black',
  'C+': 'bg-orange-500 text-white',
  C: 'bg-orange-500/80 text-white',
  D: 'bg-red-600 text-white',
}

export default function LineupDisplay({
  lineup,
  platform = 'dk',
  sport = 'nba',
  onRemovePlayer,
  baselineScore = null,
}) {
  if (!lineup) return null

  const players = lineup.players || []
  const salaryCap = lineup.salary_cap || (platform === 'dk' ? 50000 : 60000)
  const totalSalary = lineup.total_salary || 0
  const salaryPct = Math.min((totalSalary / salaryCap) * 100, 100)
  // Slot order priority: response.roster_slots > per-sport shape > FD fallback.
  // The per-sport shape is what makes NFL render 9 slots and MLB render 10.
  const slots = lineup.roster_slots
    || (platform === 'dk' ? getDkRosterSlots(sport) : FD_DISPLAY)

  // Map players to their slot positions (preserving order)
  const slotPlayers = []
  const usedIndices = new Set()
  for (const slot of slots) {
    let found = null
    for (let i = 0; i < players.length; i++) {
      if (!usedIndices.has(i) && players[i].roster_slot === slot) {
        found = players[i]
        usedIndices.add(i)
        break
      }
    }
    slotPlayers.push({ slot, player: found })
  }

  const isDK = platform === 'dk'
  const accentColor = isDK ? 'green' : 'blue'

  return (
    <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-ticker-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Target className={`w-4 h-4 ${isDK ? 'text-green-400' : 'text-blue-400'}`} />
          <h3 className="text-sm font-semibold uppercase tracking-wider">
            Optimized Lineup
          </h3>
          <span className={`px-2 py-0.5 text-xs font-bold rounded-full ${
            isDK ? 'bg-green-600/20 text-green-400' : 'bg-blue-600/20 text-blue-400'
          }`}>
            {platform.toUpperCase()}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-ticker-muted">
            {players.length}/{slots.length} filled
          </span>
          {lineup.ilp_used != null && (
            <span className={`ml-2 px-1.5 py-0.5 rounded text-[9px] font-semibold leading-none ${
              lineup.ilp_used ? 'bg-green-600/20 text-green-400' : 'bg-yellow-600/20 text-yellow-400'
            }`}>
              {lineup.ilp_used ? 'ILP Optimal' : 'Greedy'}
            </span>
          )}
        </div>
      </div>

      {/* Roster Slots */}
      <div className="divide-y divide-ticker-border/50">
        {slotPlayers.map(({ slot, player }, idx) => (
          <div
            key={`${slot}-${idx}`}
            className={`px-3 py-2 transition-colors ${
              player ? 'hover:bg-ticker-bg/30' : 'opacity-50'
            }`}
          >
            {player ? (
              <div className="flex items-center gap-2">
                {/* Slot Label */}
                <span className={`w-10 flex-shrink-0 text-xs font-bold text-center px-1.5 py-0.5 rounded ${
                  isDK ? 'bg-green-600/15 text-green-400' : 'bg-blue-600/15 text-blue-400'
                }`}>
                  {slot}
                </span>

                {/* Player Name + Team — stacked for narrow containers */}
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-semibold text-white truncate">
                    {player.player_name}
                  </div>
                  <div className="flex items-center gap-1.5 text-[10px] text-ticker-muted">
                    <span className="px-1 py-0 bg-ticker-bg rounded">{player.position}</span>
                    <span>{player.team_abbreviation}</span>
                    <span className="font-mono">${(player.salary / 1000).toFixed(1)}K</span>
                  </div>
                </div>

                {/* Projected FP — always visible */}
                <div className="flex-shrink-0 text-right">
                  <div className={`text-sm font-bold ${isDK ? 'text-green-400' : 'text-blue-400'}`}>
                    {player.projected_fp.toFixed(1)}
                  </div>
                  <div className="text-[10px] text-ticker-muted font-mono">
                    {player.floor_fp.toFixed(0)}–{player.ceiling_fp.toFixed(0)}
                  </div>
                </div>
              </div>
            ) : (
              /* Empty slot */
              <div className="flex items-center gap-2">
                <span className={`w-10 flex-shrink-0 text-xs font-bold text-center px-1.5 py-0.5 rounded ${
                  isDK ? 'bg-green-600/15 text-green-400' : 'bg-blue-600/15 text-blue-400'
                }`}>
                  {slot}
                </span>
                <span className="text-xs text-ticker-muted italic border border-dashed border-ticker-border rounded px-3 py-1">
                  Empty
                </span>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Footer Summary */}
      <div className="border-t border-ticker-border px-4 py-3 space-y-2">
        {/* Salary Bar */}
        <div className="flex items-center gap-3">
          <DollarSign className="w-3.5 h-3.5 text-ticker-muted" />
          <div className="flex-1">
            <div className="w-full bg-ticker-bg rounded-full h-2">
              <div
                className={`h-2 rounded-full transition-all duration-300 ${
                  salaryPct >= 98 ? (isDK ? 'bg-green-500' : 'bg-blue-500')
                    : salaryPct >= 85 ? 'bg-yellow-500'
                    : 'bg-ticker-muted'
                }`}
                style={{ width: `${salaryPct}%` }}
              />
            </div>
          </div>
          <span className="text-xs font-mono text-white min-w-[90px] text-right">
            ${totalSalary.toLocaleString()} / ${(salaryCap / 1000).toFixed(0)}K
          </span>
        </div>

        {/* Totals Row */}
        <div className="flex items-center justify-between text-xs">
          <div className="flex items-center gap-4">
            <span className="text-ticker-muted">
              Remaining: <span className="text-white font-mono">${(lineup.salary_remaining || 0).toLocaleString()}</span>
            </span>
            {/* Total lineup ownership */}
            {(() => {
              const totalOwn = players.reduce((sum, p) => sum + (p.estimated_ownership || 0), 0)
              return totalOwn > 0 ? (
                <span className="text-ticker-muted">
                  Own: <span className={`font-mono ${totalOwn > 150 ? 'text-red-400' : totalOwn > 100 ? 'text-yellow-400' : 'text-green-400'}`}>
                    {totalOwn.toFixed(0)}%
                  </span>
                </span>
              ) : null
            })()}
          </div>
          <div className="flex items-center gap-4">
            {lineup.quality_grade && (
              <span
                className={`px-1.5 py-0.5 rounded text-[10px] font-bold leading-none ${
                  GRADE_COLORS[lineup.quality_grade] || 'bg-ticker-border text-ticker-muted'
                }`}
                title={`Quality: ${lineup.quality_score?.toFixed(0) ?? '?'}/100`}
              >
                {lineup.quality_grade}
                {lineup.quality_score != null && (
                  <span className="font-normal opacity-70 ml-0.5">
                    {lineup.quality_score.toFixed(0)}
                  </span>
                )}
              </span>
            )}
            <span className="flex items-center gap-1 text-ticker-muted">
              <TrendingDown className="w-3 h-3" />
              Floor: <span className="text-white font-mono">{(lineup.total_floor_fp || 0).toFixed(1)}</span>
            </span>
            <span className={`flex items-center gap-1 font-bold ${isDK ? 'text-green-400' : 'text-blue-400'}`}>
              <Target className="w-3 h-3" />
              Proj: {(lineup.total_projected_fp || 0).toFixed(1)}
              {baselineScore > 0 && (
                <span className="text-[9px] font-normal text-cyan-400/80 ml-0.5">
                  ({((lineup.total_projected_fp / baselineScore) * 100).toFixed(0)}%)
                </span>
              )}
            </span>
            {/* Adjusted-FP chip (Prompt 7.2) — only shown for MLB lineups
                where the env-multiplier pass produced a total that
                differs meaningfully from the raw projected total.
                Without this, users see a Coors-heavy build with a
                seemingly low ``total_projected_fp`` and wonder why the
                optimizer chose it. */}
            {lineup.total_adjusted_fp != null
              && Math.abs(lineup.total_adjusted_fp - lineup.total_projected_fp) >= 0.5 && (
              <span
                className="flex items-center gap-1 text-[10px] font-semibold text-orange-300 bg-orange-600/10 border border-orange-600/30 rounded px-1.5 py-0.5"
                title={
                  `Park/Wind-adjusted total used by the optimizer.\n` +
                  `Raw: ${lineup.total_projected_fp.toFixed(1)}  →  ` +
                  `Adjusted: ${lineup.total_adjusted_fp.toFixed(1)}\n` +
                  `Difference reflects MLB park factors and live wind.`
                }
              >
                Adj: {lineup.total_adjusted_fp.toFixed(1)}
              </span>
            )}
            <span className="flex items-center gap-1 text-ticker-muted">
              <TrendingUp className="w-3 h-3" />
              Ceil: <span className="text-white font-mono">{(lineup.total_ceiling_fp || 0).toFixed(1)}</span>
            </span>
          </div>
        </div>
      </div>

      {/* Warnings */}
      {lineup.warnings && lineup.warnings.length > 0 && (
        <div className="border-t border-yellow-600/30 px-4 py-2 bg-yellow-900/10">
          {lineup.warnings.map((w, i) => (
            <p key={i} className="text-xs text-yellow-400">⚠ {w}</p>
          ))}
        </div>
      )}
    </div>
  )
}
