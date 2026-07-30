/**
 * LineupDetailModal — Full-screen overlay that shows detailed lineup construction.
 *
 * Triggered by double-clicking a row in LineupGrid.  Displays:
 *   - Full player names, positions, teams, salaries, projected stats
 *   - Per-player projected minutes, floor/ceiling FP, value rating
 *   - Lineup-level metrics: total salary, projected FP, floor, ceiling,
 *     salary efficiency, grade (if analysis is available)
 *   - Stat breakdown table for each player (PTS, REB, AST, STL, BLK, TO)
 */

import React, { useEffect, useRef } from 'react'
import {
  X,
  Target,
  DollarSign,
  TrendingUp,
  TrendingDown,
  Shield,
  Clock,
  BarChart3,
  Award,
} from 'lucide-react'
import EnvMultiplierBadge from './EnvMultiplierBadge'

const GRADE_COLORS = {
  'A+': 'text-green-400 bg-green-600/20 border-green-500',
  A: 'text-green-400 bg-green-600/15 border-green-500/80',
  'B+': 'text-yellow-400 bg-yellow-600/20 border-yellow-500',
  B: 'text-yellow-400 bg-yellow-600/15 border-yellow-500/80',
  'C+': 'text-orange-400 bg-orange-600/20 border-orange-500',
  C: 'text-orange-400 bg-orange-600/15 border-orange-500/80',
  D: 'text-red-400 bg-red-600/20 border-red-500',
}

const STAT_KEYS = [
  { key: 'PTS', label: 'PTS' },
  { key: 'REB', label: 'REB' },
  { key: 'AST', label: 'AST' },
  { key: 'STL', label: 'STL' },
  { key: 'BLK', label: 'BLK' },
  { key: 'TOV', label: 'TO' },
  { key: '3PM', label: '3PM' },
]

export default function LineupDetailModal({
  lineup,
  lineupIndex,
  platform = 'dk',
  analysis,
  onClose,
}) {
  const isDK = platform === 'dk'
  const overlayRef = useRef(null)

  // Close on Escape key
  useEffect(() => {
    const handleKey = (e) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [onClose])

  // Close when clicking the backdrop
  const handleBackdropClick = (e) => {
    if (e.target === overlayRef.current) onClose()
  }

  if (!lineup) return null

  const players = lineup.players || []
  const salaryCap = lineup.salary_cap || (isDK ? 50000 : 60000)
  const salaryPct = ((lineup.total_salary / salaryCap) * 100).toFixed(1)

  // Grab analysis for this lineup if available
  const lineupAnalysis = analysis?.analyses?.find(
    (a) => a.lineup_index === lineupIndex
  )
  // Use analysis grade if available, otherwise fall back to optimizer quality grade
  const grade = lineupAnalysis?.overall_grade || lineup.quality_grade
  const score = lineupAnalysis?.overall_score ?? lineup.quality_score
  const gradeClass = GRADE_COLORS[grade] || 'text-ticker-muted bg-ticker-bg border-ticker-border'

  // Check if any player has projected_stats
  const hasStats = players.some((p) => p.projected_stats && Object.keys(p.projected_stats).length > 0)

  // Compute per-stat totals
  const statTotals = {}
  if (hasStats) {
    for (const { key } of STAT_KEYS) {
      statTotals[key] = players.reduce((sum, p) => {
        const val = p.projected_stats?.[key] ?? 0
        return sum + val
      }, 0)
    }
  }

  return (
    <div
      ref={overlayRef}
      onClick={handleBackdropClick}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4"
    >
      <div className="bg-ticker-card border border-ticker-border rounded-xl shadow-2xl w-full max-w-4xl max-h-[90vh] overflow-hidden flex flex-col">
        {/* ── Header ─────────────────────────────────────────── */}
        <div className="px-6 py-4 border-b border-ticker-border flex items-center justify-between flex-shrink-0">
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2">
              <Target className={`w-5 h-5 ${isDK ? 'text-green-400' : 'text-blue-400'}`} />
              <h2 className="text-lg font-bold text-white">
                Lineup #{lineupIndex + 1}
              </h2>
              <span className={`px-2.5 py-0.5 text-xs font-bold rounded-full ${
                isDK ? 'bg-green-600/20 text-green-400' : 'bg-blue-600/20 text-blue-400'
              }`}>
                {platform.toUpperCase()}
              </span>
            </div>

            {/* Grade badge */}
            {grade && (
              <div className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border ${gradeClass}`}>
                <Award className="w-4 h-4" />
                <span className="text-lg font-black">{grade}</span>
                <span className="text-xs opacity-70">{score}/100</span>
              </div>
            )}
          </div>

          <button
            onClick={onClose}
            className="p-2 rounded-lg hover:bg-ticker-bg transition-colors text-ticker-muted hover:text-white"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* ── Player Table ────────────────────────────────────── */}
        <div className="flex-1 overflow-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 z-10">
              <tr className="bg-ticker-bg border-b border-ticker-border">
                <th className="px-4 py-3 text-left text-[10px] font-semibold uppercase tracking-wider text-ticker-muted">
                  Slot
                </th>
                <th className="px-4 py-3 text-left text-[10px] font-semibold uppercase tracking-wider text-ticker-muted">
                  Player
                </th>
                <th className="px-3 py-3 text-center text-[10px] font-semibold uppercase tracking-wider text-ticker-muted">
                  Pos
                </th>
                <th className="px-3 py-3 text-center text-[10px] font-semibold uppercase tracking-wider text-ticker-muted">
                  Team
                </th>
                <th className="px-3 py-3 text-right text-[10px] font-semibold uppercase tracking-wider text-ticker-muted">
                  Salary
                </th>
                <th className="px-3 py-3 text-right text-[10px] font-semibold uppercase tracking-wider text-ticker-muted">
                  <Clock className="w-3 h-3 inline mr-0.5 -mt-0.5" />Min
                </th>
                <th className={`px-3 py-3 text-right text-[10px] font-semibold uppercase tracking-wider ${isDK ? 'text-green-400' : 'text-blue-400'}`}>
                  Proj
                </th>
                <th className="px-3 py-3 text-right text-[10px] font-semibold uppercase tracking-wider text-ticker-muted">
                  Floor
                </th>
                <th className="px-3 py-3 text-right text-[10px] font-semibold uppercase tracking-wider text-ticker-muted">
                  Ceil
                </th>
                <th className="px-3 py-3 text-right text-[10px] font-semibold uppercase tracking-wider text-ticker-muted">
                  Value
                </th>
                {/* Stat columns */}
                {hasStats &&
                  STAT_KEYS.map(({ label }) => (
                    <th
                      key={label}
                      className="px-2 py-3 text-right text-[10px] font-semibold uppercase tracking-wider text-ticker-muted"
                    >
                      {label}
                    </th>
                  ))}
              </tr>
            </thead>
            <tbody>
              {players.map((p, idx) => {
                const value =
                  p.salary > 0
                    ? ((p.projected_fp / p.salary) * 1000).toFixed(1)
                    : '0.0'
                const isHighValue = parseFloat(value) >= 5.0
                const isLowMin = p.projected_minutes < 20

                return (
                  <tr
                    key={p.player_id || idx}
                    className={`border-b border-ticker-border/30 transition-colors hover:bg-ticker-bg/30 ${
                      idx % 2 === 0 ? '' : 'bg-ticker-bg/10'
                    }`}
                  >
                    {/* Slot */}
                    <td className="px-4 py-2.5">
                      <span
                        className={`inline-block px-2 py-0.5 rounded text-[10px] font-bold ${
                          isDK
                            ? 'bg-green-600/15 text-green-400'
                            : 'bg-blue-600/15 text-blue-400'
                        }`}
                      >
                        {p.roster_slot}
                      </span>
                    </td>

                    {/* Full player name */}
                    <td className="px-4 py-2.5">
                      <span className="font-semibold text-white">
                        {p.player_name}
                      </span>
                    </td>

                    {/* Position */}
                    <td className="px-3 py-2.5 text-center">
                      <span className="px-1.5 py-0.5 bg-ticker-bg rounded text-[10px] font-semibold text-ticker-muted">
                        {p.position}
                      </span>
                    </td>

                    {/* Team */}
                    <td className="px-3 py-2.5 text-center text-xs font-semibold text-white">
                      {p.team_abbreviation}
                    </td>

                    {/* Salary */}
                    <td className="px-3 py-2.5 text-right font-mono text-white">
                      ${p.salary.toLocaleString()}
                    </td>

                    {/* Minutes */}
                    <td className={`px-3 py-2.5 text-right font-mono ${isLowMin ? 'text-yellow-400' : 'text-ticker-muted'}`}>
                      {p.projected_minutes.toFixed(1)}
                    </td>

                    {/* Projected FP — with park/wind badge (Prompt 7.4)
                        when the player is in an env-adjusted game. Same
                        chip the player pool table uses, so users see
                        the same visual everywhere a projection is
                        shown. The chip self-hides for non-MLB sports. */}
                    <td className={`px-3 py-2.5 text-right font-bold ${isDK ? 'text-green-400' : 'text-blue-400'}`}>
                      <div className="flex items-center justify-end gap-1">
                        {p.projected_fp.toFixed(1)}
                        <EnvMultiplierBadge player={p} size="sm" />
                      </div>
                    </td>

                    {/* Floor */}
                    <td className="px-3 py-2.5 text-right font-mono text-ticker-muted">
                      {p.floor_fp.toFixed(1)}
                    </td>

                    {/* Ceiling */}
                    <td className="px-3 py-2.5 text-right font-mono text-ticker-muted">
                      {p.ceiling_fp.toFixed(1)}
                    </td>

                    {/* Value (FP per $1K) */}
                    <td className={`px-3 py-2.5 text-right font-mono ${isHighValue ? 'text-green-400 font-bold' : 'text-ticker-muted'}`}>
                      {value}x
                    </td>

                    {/* Projected stat columns */}
                    {hasStats &&
                      STAT_KEYS.map(({ key }) => (
                        <td
                          key={key}
                          className="px-2 py-2.5 text-right font-mono text-xs text-ticker-muted"
                        >
                          {p.projected_stats?.[key] != null
                            ? p.projected_stats[key].toFixed(1)
                            : '—'}
                        </td>
                      ))}
                  </tr>
                )
              })}

              {/* ── Totals row ──────────────────────────────────── */}
              <tr className="border-t-2 border-ticker-border bg-ticker-bg/50 font-semibold">
                <td className="px-4 py-3 text-xs text-ticker-muted" colSpan={2}>
                  TOTALS
                </td>
                <td className="px-3 py-3" />
                <td className="px-3 py-3" />
                <td className="px-3 py-3 text-right font-mono text-white">
                  ${lineup.total_salary.toLocaleString()}
                </td>
                <td className="px-3 py-3 text-right font-mono text-ticker-muted">
                  {players.reduce((s, p) => s + p.projected_minutes, 0).toFixed(1)}
                </td>
                <td className={`px-3 py-3 text-right font-bold ${isDK ? 'text-green-400' : 'text-blue-400'}`}>
                  <div className="leading-tight">
                    {lineup.total_projected_fp.toFixed(1)}
                    {/* Adjusted total (Prompt 7.4): the env-aware
                        number the optimizer actually maximised. Hidden
                        for non-MLB lineups (total_adjusted_fp=null)
                        and for tiny deltas (< 0.5 FP). */}
                    {lineup.total_adjusted_fp != null
                      && Math.abs(lineup.total_adjusted_fp - lineup.total_projected_fp) >= 0.5 && (
                      <div
                        className="text-[10px] font-bold text-orange-300"
                        title={
                          `Park/Wind-adjusted total used by the optimizer.\n` +
                          `Raw: ${lineup.total_projected_fp.toFixed(1)}  →  ` +
                          `Adjusted: ${lineup.total_adjusted_fp.toFixed(1)}`
                        }
                      >
                        Adj {lineup.total_adjusted_fp.toFixed(1)}
                      </div>
                    )}
                  </div>
                </td>
                <td className="px-3 py-3 text-right font-mono text-ticker-muted">
                  {lineup.total_floor_fp.toFixed(1)}
                </td>
                <td className="px-3 py-3 text-right font-mono text-ticker-muted">
                  {lineup.total_ceiling_fp.toFixed(1)}
                </td>
                <td className="px-3 py-3 text-right font-mono text-ticker-muted">
                  {lineup.total_salary > 0
                    ? ((lineup.total_projected_fp / lineup.total_salary) * 1000).toFixed(1)
                    : '0.0'}x
                </td>
                {hasStats &&
                  STAT_KEYS.map(({ key }) => (
                    <td
                      key={key}
                      className="px-2 py-3 text-right font-mono text-xs text-white"
                    >
                      {statTotals[key]?.toFixed(1) ?? '—'}
                    </td>
                  ))}
              </tr>
            </tbody>
          </table>
        </div>

        {/* ── Footer metrics ─────────────────────────────────── */}
        <div className="px-6 py-4 border-t border-ticker-border flex-shrink-0">
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
            {/* Salary */}
            <div className="bg-ticker-bg/50 rounded-lg px-3 py-2.5 text-center">
              <div className="flex items-center justify-center gap-1 mb-1">
                <DollarSign className="w-3 h-3 text-ticker-muted" />
                <span className="text-[10px] font-semibold uppercase text-ticker-muted">Salary</span>
              </div>
              <div className="text-sm font-bold text-white">
                ${(lineup.total_salary / 1000).toFixed(1)}K
              </div>
              <div className="text-[10px] text-ticker-muted">
                {salaryPct}% of ${(salaryCap / 1000).toFixed(0)}K cap
              </div>
            </div>

            {/* Remaining */}
            <div className="bg-ticker-bg/50 rounded-lg px-3 py-2.5 text-center">
              <div className="flex items-center justify-center gap-1 mb-1">
                <DollarSign className="w-3 h-3 text-ticker-muted" />
                <span className="text-[10px] font-semibold uppercase text-ticker-muted">Remaining</span>
              </div>
              <div className={`text-sm font-bold ${
                lineup.salary_remaining <= 500 ? 'text-green-400' :
                lineup.salary_remaining <= 2000 ? 'text-yellow-400' : 'text-red-400'
              }`}>
                ${lineup.salary_remaining.toLocaleString()}
              </div>
              <div className="text-[10px] text-ticker-muted">
                {lineup.salary_remaining <= 500 ? 'Optimal' : lineup.salary_remaining <= 2000 ? 'Good' : 'Wasted cap'}
              </div>
            </div>

            {/* Projected */}
            <div className="bg-ticker-bg/50 rounded-lg px-3 py-2.5 text-center">
              <div className="flex items-center justify-center gap-1 mb-1">
                <Target className={`w-3 h-3 ${isDK ? 'text-green-400' : 'text-blue-400'}`} />
                <span className="text-[10px] font-semibold uppercase text-ticker-muted">Projected</span>
              </div>
              <div className={`text-sm font-bold ${isDK ? 'text-green-400' : 'text-blue-400'}`}>
                {lineup.total_projected_fp.toFixed(1)} FP
              </div>
              <div className="text-[10px] text-ticker-muted">
                {(lineup.total_projected_fp / players.length).toFixed(1)} per player
              </div>
            </div>

            {/* Adjusted (Prompt 7.4) — dedicated metric card matching
                the rest of the footer's visual rhythm. Renders only
                when an env-aware total exists and meaningfully differs
                from the raw total. The "explains why this lineup was
                drafted" framing is the entire point of the prompt. */}
            {lineup.total_adjusted_fp != null
              && Math.abs(lineup.total_adjusted_fp - lineup.total_projected_fp) >= 0.5 && (
              <div className="bg-ticker-bg/50 rounded-lg px-3 py-2.5 text-center border border-orange-600/30">
                <div className="flex items-center justify-center gap-1 mb-1">
                  <Target className="w-3 h-3 text-orange-300" />
                  <span className="text-[10px] font-semibold uppercase text-orange-300">Adjusted</span>
                </div>
                <div className="text-sm font-bold text-orange-300">
                  {lineup.total_adjusted_fp.toFixed(1)} FP
                </div>
                <div
                  className="text-[10px] text-ticker-muted"
                  title="Park/Wind multiplier folded across the lineup"
                >
                  {lineup.total_adjusted_fp > lineup.total_projected_fp ? '+' : ''}
                  {(lineup.total_adjusted_fp - lineup.total_projected_fp).toFixed(1)} vs raw
                </div>
              </div>
            )}

            {/* Floor */}
            <div className="bg-ticker-bg/50 rounded-lg px-3 py-2.5 text-center">
              <div className="flex items-center justify-center gap-1 mb-1">
                <Shield className="w-3 h-3 text-ticker-muted" />
                <span className="text-[10px] font-semibold uppercase text-ticker-muted">Floor</span>
              </div>
              <div className="text-sm font-bold text-white">
                {lineup.total_floor_fp.toFixed(1)} FP
              </div>
              <div className="text-[10px] text-ticker-muted">
                {lineup.total_projected_fp > 0
                  ? `${((lineup.total_floor_fp / lineup.total_projected_fp) * 100).toFixed(0)}% of proj`
                  : '—'}
              </div>
            </div>

            {/* Ceiling */}
            <div className="bg-ticker-bg/50 rounded-lg px-3 py-2.5 text-center">
              <div className="flex items-center justify-center gap-1 mb-1">
                <TrendingUp className="w-3 h-3 text-ticker-muted" />
                <span className="text-[10px] font-semibold uppercase text-ticker-muted">Ceiling</span>
              </div>
              <div className="text-sm font-bold text-white">
                {lineup.total_ceiling_fp.toFixed(1)} FP
              </div>
              <div className="text-[10px] text-ticker-muted">
                {lineup.total_projected_fp > 0
                  ? `${((lineup.total_ceiling_fp / lineup.total_projected_fp) * 100).toFixed(0)}% of proj`
                  : '—'}
              </div>
            </div>

            {/* Overall Value */}
            <div className="bg-ticker-bg/50 rounded-lg px-3 py-2.5 text-center">
              <div className="flex items-center justify-center gap-1 mb-1">
                <BarChart3 className="w-3 h-3 text-ticker-muted" />
                <span className="text-[10px] font-semibold uppercase text-ticker-muted">Avg Value</span>
              </div>
              <div className="text-sm font-bold text-white">
                {lineup.total_salary > 0
                  ? ((lineup.total_projected_fp / lineup.total_salary) * 1000).toFixed(2)
                  : '0.00'}x
              </div>
              <div className="text-[10px] text-ticker-muted">
                FP per $1K
              </div>
            </div>
          </div>

          {/* Analysis dimension scores (if available) */}
          {lineupAnalysis?.dimension_scores && (
            <div className="mt-3 pt-3 border-t border-ticker-border/50">
              <div className="flex items-center gap-4 flex-wrap">
                {lineupAnalysis.dimension_scores.map((dim) => {
                  const pct = (dim.score / 10) * 100
                  return (
                    <div key={dim.dimension} className="flex items-center gap-2 min-w-[140px]">
                      <span className="text-[10px] font-semibold uppercase text-ticker-muted w-16 truncate">
                        {dim.dimension.replace('_', ' ')}
                      </span>
                      <div className="flex-1 bg-ticker-border rounded-full h-1.5 min-w-[50px]">
                        <div
                          className={`h-1.5 rounded-full transition-all ${
                            dim.score >= 8
                              ? isDK ? 'bg-green-500' : 'bg-blue-500'
                              : dim.score >= 6 ? 'bg-yellow-500'
                              : dim.score >= 4 ? 'bg-orange-500'
                              : 'bg-red-500'
                          }`}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                      <span
                        className={`text-[10px] font-bold min-w-[24px] text-right ${
                          dim.score >= 8 ? 'text-green-400'
                          : dim.score >= 6 ? 'text-yellow-400'
                          : dim.score >= 4 ? 'text-orange-400'
                          : 'text-red-400'
                        }`}
                      >
                        {dim.score.toFixed(1)}
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Risks (if any) */}
          {lineupAnalysis?.risks?.length > 0 && (
            <div className="mt-3 pt-3 border-t border-ticker-border/50 flex items-center gap-2 flex-wrap">
              {lineupAnalysis.risks.map((risk, i) => (
                <span
                  key={i}
                  className={`px-2 py-1 rounded text-[10px] font-semibold ${
                    risk.severity === 'high'
                      ? 'bg-red-600/15 text-red-400'
                      : risk.severity === 'medium'
                      ? 'bg-yellow-600/15 text-yellow-400'
                      : 'bg-green-600/15 text-green-400'
                  }`}
                >
                  {risk.description}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
