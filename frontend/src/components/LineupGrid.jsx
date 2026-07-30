/**
 * LineupGrid â€” Compact table view for multiple generated DFS lineups.
 *
 * Each row represents one lineup.  Columns show roster slots, salary,
 * and projected FP.  Clicking a row selects it for detailed analysis.
 *
 * Supports pagination for large lineup sets (50+), displays grade
 * distribution summary, and sortable columns (grade, proj, floor, ceil, salary).
 */

import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { Trophy, ChevronLeft, ChevronRight, ArrowUp, ArrowUpDown, ArrowDownUp } from 'lucide-react'
import LineupDetailModal from './LineupDetailModal'
import { getDkRosterSlots } from '../config/sportShapes'

const FD_SLOTS = ['PG1', 'PG2', 'SG1', 'SG2', 'SF1', 'SF2', 'PF1', 'PF2', 'C']

const PAGE_SIZE = 25

const GRADE_COLORS = {
  'A+': 'bg-green-600 text-white',
  A: 'bg-green-600/80 text-white',
  'B+': 'bg-yellow-500 text-black',
  B: 'bg-yellow-500/80 text-black',
  'C+': 'bg-orange-500 text-white',
  C: 'bg-orange-500/80 text-white',
  D: 'bg-red-600 text-white',
}

const GRADE_BAR_COLORS = {
  'A+': 'bg-green-500',
  A: 'bg-green-400',
  'B+': 'bg-yellow-400',
  B: 'bg-yellow-500',
  'C+': 'bg-orange-400',
  C: 'bg-orange-500',
  D: 'bg-red-500',
}

// Numeric grade values for sorting (higher = better)
const GRADE_SORT_VALUE = { 'A+': 7, A: 6, 'B+': 5, B: 4, 'C+': 3, C: 2, D: 1 }

function getLastName(fullName) {
  const parts = fullName.split(' ')
  return parts.length > 1 ? parts.slice(1).join(' ') : fullName
}

export default function LineupGrid({
  lineups,
  platform = 'dk',
  sport = 'nba',
  selectedIndex = 0,
  onSelect,
  analysis,
  refinementResults,
  baselineScore = null,
  baselineLineup = null,
  sortConfig: externalSortConfig,
  onSortChange,
}) {
  // Toggle for the "Optimal" badge — expands to show the actual players that
  // make up the theoretical-max lineup, not just the FP score.
  const [showBaselineLineup, setShowBaselineLineup] = useState(false)
  const isDK = platform === 'dk'
  // Slot order, in priority:
  //   1. ``lineup.roster_slots`` from the first generated lineup — authoritative
  //      shape from the backend's actual draft group.
  //   2. Per-sport shape from sportShapes.js — used before any lineup is
  //      generated. This is what makes NFL show 9 slots (QB/RB/RB/WR/WR/WR/
  //      TE/FLEX/DST) and MLB show 10 (P/P/C/1B/2B/3B/SS/OF/OF/OF) instead
  //      of the legacy 8-slot NBA default.
  //   3. FD fallback — FD-only path; rarely hit in NFL/MLB which are DK-first.
  const slots = (lineups.length && lineups[0].roster_slots)
    || (isDK ? getDkRosterSlots(sport) : FD_SLOTS)

  // Sort state: use external (from useLineupState) if provided, else local fallback
  const [localSortConfig, setLocalSortConfig] = useState(null)
  const sortConfig = externalSortConfig !== undefined ? externalSortConfig : localSortConfig
  const setSortConfig = onSortChange || setLocalSortConfig

  // Pagination state
  const [page, setPage] = useState(0)

  // Reset to first page when lineups change (e.g. new generation)
  useEffect(() => { setPage(0) }, [lineups.length])

  // Modal state for double-click detail view
  const [modalIndex, setModalIndex] = useState(null)
  const showModal = modalIndex !== null && lineups[modalIndex]

  // Map grades by original lineup index.
  // Priority: analysis grade > optimizer quality_grade from lineup object.
  const grades = useMemo(() => {
    const map = {}
    // First, populate from optimizer quality grades (always available)
    lineups.forEach((lu, idx) => {
      if (lu.quality_grade) {
        map[idx] = {
          grade: lu.quality_grade,
          score: lu.quality_score ?? 0,
          source: 'quality',
        }
      }
    })
    // Then, override with analysis grades (higher fidelity)
    if (analysis?.analyses) {
      for (const a of analysis.analyses) {
        map[a.lineup_index] = {
          grade: a.overall_grade,
          score: a.overall_score,
          source: 'analysis',
        }
      }
    }
    return map
  }, [analysis, lineups])

  // Build refinement lookup: lineup_index â†’ { original_grade, refined_grade }
  const refineLookup = useMemo(() => {
    const map = {}
    if (refinementResults) {
      for (const r of refinementResults) {
        if (r.refined_score > r.original_score) {
          map[r.lineup_index] = {
            originalGrade: r.original_grade,
            refinedGrade: r.refined_grade,
            scoreDelta: +(r.refined_score - r.original_score).toFixed(1),
          }
        }
      }
    }
    return map
  }, [refinementResults])

  // Build a sorted index array: each entry is an original index into lineups[]
  const sortedIndices = useMemo(() => {
    const indices = lineups.map((_, i) => i)

    if (!sortConfig) return indices

    const { key, dir } = sortConfig
    const mult = dir === 'desc' ? -1 : 1

    indices.sort((a, b) => {
      let va, vb
      const la = lineups[a]
      const lb = lineups[b]

      switch (key) {
        case 'grade':
          va = grades[a]?.score ?? -1
          vb = grades[b]?.score ?? -1
          break
        case 'proj':
          va = la.total_projected_fp
          vb = lb.total_projected_fp
          break
        case 'floor':
          va = la.total_floor_fp
          vb = lb.total_floor_fp
          break
        case 'ceil':
          va = la.total_ceiling_fp
          vb = lb.total_ceiling_fp
          break
        case 'salary':
          va = la.total_salary
          vb = lb.total_salary
          break
        default:
          return 0
      }
      if (va < vb) return -1 * mult
      if (va > vb) return 1 * mult
      return 0
    })

    return indices
  }, [lineups, sortConfig, grades])

  // Pagination computed from sorted indices
  const totalPages = Math.ceil(sortedIndices.length / PAGE_SIZE)
  const needsPagination = sortedIndices.length > PAGE_SIZE
  const pageStart = page * PAGE_SIZE
  const pageEnd = Math.min(pageStart + PAGE_SIZE, sortedIndices.length)
  const visibleIndices = sortedIndices.slice(pageStart, pageEnd)

  // Find which page the selected lineup is on (in sorted order)
  const selectedSortPos = sortedIndices.indexOf(selectedIndex)
  const selectedPage = selectedSortPos >= 0 ? Math.floor(selectedSortPos / PAGE_SIZE) : 0

  // Grade distribution for summary bar
  const gradeDist = useMemo(() => {
    const dist = {}
    const gradeOrder = ['A+', 'A', 'B+', 'B', 'C+', 'C', 'D']
    for (const g of gradeOrder) dist[g] = 0
    for (const g of Object.values(grades)) {
      if (dist[g.grade] !== undefined) dist[g.grade]++
    }
    return dist
  }, [grades])

  const hasGrades = Object.values(gradeDist).some((v) => v > 0)

  // Compute averages
  const avgSalary = lineups.length
    ? Math.round(lineups.reduce((s, l) => s + l.total_salary, 0) / lineups.length)
    : 0
  const avgFP = lineups.length
    ? (lineups.reduce((s, l) => s + l.total_projected_fp, 0) / lineups.length).toFixed(1)
    : '0.0'
  const avgFloor = lineups.length
    ? (lineups.reduce((s, l) => s + l.total_floor_fp, 0) / lineups.length).toFixed(1)
    : '0.0'
  const avgCeil = lineups.length
    ? (lineups.reduce((s, l) => s + l.total_ceiling_fp, 0) / lineups.length).toFixed(1)
    : '0.0'

  const handlePageChange = (newPage) => {
    setPage(Math.max(0, Math.min(newPage, totalPages - 1)))
  }

  const goToSelectedPage = () => {
    setPage(selectedPage)
  }

  // Column sort handler â€” cycles: desc â†’ asc â†’ none
  const handleSort = useCallback((key) => {
    setSortConfig((prev) => {
      if (!prev || prev.key !== key) {
        // First click on a new column â†’ sort descending (best first)
        return { key, dir: 'desc' }
      }
      if (prev.dir === 'desc') {
        // Second click â†’ ascending
        return { key, dir: 'asc' }
      }
      // Third click â†’ clear sort
      return null
    })
    setPage(0) // reset to page 1 on sort change
  }, [])

  // Sort indicator component
  const SortIcon = ({ colKey }) => {
    if (!sortConfig || sortConfig.key !== colKey) {
      return <ArrowUpDown className="w-2.5 h-2.5 opacity-30 inline ml-0.5" />
    }
    return sortConfig.dir === 'desc'
      ? <ArrowDownUp className="w-2.5 h-2.5 inline ml-0.5" />
      : <ArrowUp className="w-2.5 h-2.5 inline ml-0.5" />
  }

  return (
    <>
    {/* Detail modal overlay */}
    {showModal && (
      <LineupDetailModal
        lineup={lineups[modalIndex]}
        lineupIndex={modalIndex}
        platform={platform}
        analysis={analysis}
        onClose={() => setModalIndex(null)}
      />
    )}

    <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-ticker-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Trophy className={`w-4 h-4 ${isDK ? 'text-green-400' : 'text-blue-400'}`} />
          <h3 className="text-sm font-semibold uppercase tracking-wider">
            Generated Lineups
          </h3>
          <span
            className={`px-2 py-0.5 text-xs font-bold rounded-full ${
              isDK ? 'bg-green-600/20 text-green-400' : 'bg-blue-600/20 text-blue-400'
            }`}
          >
            {lineups.length}
          </span>
          {/* Sort active indicator */}
          {sortConfig && (
            <button
              onClick={() => { setSortConfig(null); setPage(0) }}
              className="px-1.5 py-0.5 text-[9px] font-semibold rounded bg-ticker-bg border border-ticker-border text-ticker-muted hover:text-white hover:border-ticker-muted transition-colors"
              title="Clear sort"
            >
              Sorted by {sortConfig.key} {sortConfig.dir === 'desc' ? 'â†“' : 'â†‘'} Â· âœ•
            </button>
          )}
        </div>
        <div className="flex items-center gap-3">
          {/* Optimal baseline score — click to reveal the actual lineup */}
          {baselineScore != null && baselineScore > 0 && (
            <button
              type="button"
              onClick={() => setShowBaselineLineup((v) => !v)}
              disabled={!baselineLineup}
              className={`flex items-center gap-1 px-2 py-0.5 rounded bg-cyan-600/15 border border-cyan-600/30 ${
                baselineLineup
                  ? 'hover:bg-cyan-600/25 cursor-pointer'
                  : 'cursor-default opacity-90'
              }`}
              title={
                baselineLineup
                  ? 'Click to show / hide the optimal lineup players'
                  : 'Optimal unconstrained lineup projection (100% ceiling)'
              }
            >
              <span className="text-[10px] text-cyan-400/70 font-medium">Optimal</span>
              <span className="text-[11px] text-cyan-400 font-bold">{baselineScore.toFixed(1)}</span>
              <span className="text-[10px] text-cyan-400/70">FP</span>
              {baselineLineup && (
                <span className="text-[9px] text-cyan-400/60 ml-0.5">
                  {showBaselineLineup ? '▲' : '▼'}
                </span>
              )}
            </button>
          )}
          {/* Grade distribution mini-bar */}
          {hasGrades && (
            <div className="flex items-center gap-1.5">
              {Object.entries(gradeDist).map(([grade, count]) =>
                count > 0 ? (
                  <span
                    key={grade}
                    className={`px-1 py-0.5 rounded text-[9px] font-bold leading-none ${
                      GRADE_COLORS[grade] || 'bg-ticker-border text-ticker-muted'
                    }`}
                    title={`${count} lineup${count > 1 ? 's' : ''} graded ${grade}`}
                  >
                    {grade}Ã—{count}
                  </span>
                ) : null
              )}
            </div>
          )}
          <span className="text-xs text-ticker-muted">
            Click to select Â· Double-click for details
          </span>
        </div>
      </div>

      {/* Optimal-lineup detail panel — toggled by the Optimal badge */}
      {showBaselineLineup && baselineLineup && (
        <div className="mb-2 px-3 py-2 rounded border border-cyan-600/30 bg-cyan-600/5">
          <div className="flex items-center justify-between mb-1.5">
            <div className="flex items-center gap-2">
              <span className="text-[11px] font-semibold text-cyan-400">
                Optimal Lineup
              </span>
              <span className="text-[10px] text-ticker-muted">
                Theoretical max — no GPP constraints, no noise
              </span>
            </div>
            <div className="flex items-center gap-3 text-[10px] text-ticker-muted">
              <span>
                Salary{' '}
                <span className="text-white font-medium">
                  ${baselineLineup.total_salary?.toLocaleString?.() ?? baselineLineup.total_salary}
                </span>
                {' / '}
                <span>${baselineLineup.salary_cap?.toLocaleString?.() ?? baselineLineup.salary_cap}</span>
              </span>
              <span>
                Floor{' '}
                <span className="text-white font-medium">
                  {baselineLineup.total_floor_fp?.toFixed?.(1) ?? '—'}
                </span>
              </span>
              <span>
                Ceil{' '}
                <span className="text-white font-medium">
                  {baselineLineup.total_ceiling_fp?.toFixed?.(1) ?? '—'}
                </span>
              </span>
              <span>
                Proj{' '}
                <span className="text-cyan-400 font-bold">
                  {baselineLineup.total_projected_fp?.toFixed?.(1) ?? baselineScore.toFixed(1)}
                </span>
              </span>
            </div>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-x-3 gap-y-1">
            {(baselineLineup.players || []).map((p, i) => (
              <div
                key={`${p.player_id}-${i}`}
                className="flex items-center justify-between text-[11px]"
              >
                <span className="flex items-center gap-1.5 min-w-0">
                  <span
                    className={`px-1 py-0.5 rounded text-[9px] font-bold leading-none ${
                      isDK
                        ? 'bg-green-600/15 text-green-400'
                        : 'bg-blue-600/15 text-blue-400'
                    }`}
                  >
                    {p.roster_slot}
                  </span>
                  <span className="text-white truncate" title={p.player_name}>
                    {p.player_name}
                  </span>
                  <span className="text-ticker-muted text-[10px]">
                    {p.team_abbreviation}
                  </span>
                </span>
                <span className="text-ticker-muted text-[10px] flex items-center gap-1.5 shrink-0 ml-1">
                  <span>${p.salary?.toLocaleString?.() ?? p.salary}</span>
                  <span className="text-cyan-400 font-medium">
                    {p.projected_fp?.toFixed?.(1)}
                  </span>
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-ticker-border bg-ticker-bg/50">
              <th
                className="px-2 py-2 text-left w-10 sticky left-0 bg-ticker-bg/50 cursor-pointer select-none"
                onClick={() => hasGrades && handleSort('grade')}
                title={hasGrades ? 'Sort by grade' : ''}
              >
                #
                {hasGrades && <SortIcon colKey="grade" />}
              </th>
              {slots.map((slot) => (
                <th
                  key={slot}
                  className="px-1.5 py-2 text-center min-w-[80px]"
                >
                  <span
                    className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                      isDK
                        ? 'bg-green-600/15 text-green-400'
                        : 'bg-blue-600/15 text-blue-400'
                    }`}
                  >
                    {slot}
                  </span>
                </th>
              ))}
              <th
                className="px-2 py-2 text-right min-w-[65px] cursor-pointer select-none"
                onClick={() => handleSort('salary')}
                title="Sort by salary"
              >
                Salary<SortIcon colKey="salary" />
              </th>
              <th
                className="px-2 py-2 text-right min-w-[55px] cursor-pointer select-none"
                onClick={() => handleSort('proj')}
                title="Sort by projected FP"
              >
                <span className={isDK ? 'text-green-400' : 'text-blue-400'}>
                  Proj
                </span>
                <SortIcon colKey="proj" />
              </th>
              <th
                className="px-2 py-2 text-right min-w-[50px] cursor-pointer select-none"
                onClick={() => handleSort('floor')}
                title="Sort by floor FP"
              >
                Floor<SortIcon colKey="floor" />
              </th>
              <th
                className="px-2 py-2 text-right min-w-[50px] cursor-pointer select-none"
                onClick={() => handleSort('ceil')}
                title="Sort by ceiling FP"
              >
                Ceil<SortIcon colKey="ceil" />
              </th>
              <th className="px-2 py-2 text-right min-w-[50px]">
                Own%
              </th>
            </tr>
          </thead>
          <tbody>
            {visibleIndices.map((origIdx, visIdx) => {
              const lineup = lineups[origIdx]
              const isSelected = origIdx === selectedIndex
              const gradeInfo = grades[origIdx]
              const refineInfo = refineLookup[origIdx]

              return (
                <tr
                  key={origIdx}
                  onClick={() => onSelect?.(origIdx)}
                  onDoubleClick={() => setModalIndex(origIdx)}
                  className={`border-b border-ticker-border/30 cursor-pointer transition-colors ${
                    isSelected
                      ? isDK
                        ? 'bg-green-900/20 border-l-2 border-l-green-500'
                        : 'bg-blue-900/20 border-l-2 border-l-blue-500'
                      : visIdx % 2 === 0
                      ? 'bg-ticker-card hover:bg-ticker-bg/30'
                      : 'bg-ticker-bg/20 hover:bg-ticker-bg/30'
                  }`}
                >
                  {/* Lineup number + grade */}
                  <td className="px-2 py-1.5 sticky left-0 bg-inherit">
                    <div className="flex items-center gap-1">
                      <span className="text-ticker-muted font-mono text-[11px]">
                        {origIdx + 1}
                      </span>
                      {gradeInfo && (
                        <span
                          className={`px-1 py-0.5 rounded text-[9px] font-bold leading-none ${
                            GRADE_COLORS[gradeInfo.grade] || 'bg-ticker-border text-ticker-muted'
                          }`}
                          title={`Score: ${gradeInfo.score?.toFixed?.(0) ?? gradeInfo.score}/100`}
                        >
                          {gradeInfo.grade}
                          <span className="font-normal opacity-60 ml-0.5">
                            {typeof gradeInfo.score === 'number' ? gradeInfo.score.toFixed(0) : ''}
                          </span>
                        </span>
                      )}
                      {refineInfo && (
                        <span
                          className="text-[9px] text-green-400 flex items-center gap-0.5"
                          title={`Improved from ${refineInfo.originalGrade} (+${refineInfo.scoreDelta})`}
                        >
                          <ArrowUp className="w-2.5 h-2.5" />
                        </span>
                      )}
                    </div>
                  </td>

                  {/* Player cells */}
                  {slots.map((slot, slotIdx) => {
                    const player = lineup.players[slotIdx]
                    if (!player) {
                      return (
                        <td
                          key={slot}
                          className="px-1.5 py-1.5 text-center text-ticker-muted"
                        >
                          â€”
                        </td>
                      )
                    }

                    return (
                      <td
                        key={slot}
                        className="px-1.5 py-1.5 text-center"
                      >
                        <div className="leading-tight">
                          <div className="font-semibold text-white truncate max-w-[90px]">
                            {getLastName(player.player_name)}
                          </div>
                          <div className="text-[10px] text-ticker-muted font-mono">
                            ${(player.salary / 1000).toFixed(1)}K
                          </div>
                        </div>
                      </td>
                    )
                  })}

                  {/* Totals */}
                  <td className="px-2 py-1.5 text-right font-mono text-white">
                    ${(lineup.total_salary / 1000).toFixed(1)}K
                  </td>
                  <td
                    className={`px-2 py-1.5 text-right font-bold ${
                      isDK ? 'text-green-400' : 'text-blue-400'
                    }`}
                  >
                    <div className="leading-tight">
                      {lineup.total_projected_fp.toFixed(1)}
                      {/* Adjusted total (Prompt 7.4) — the env-aware
                          number the optimizer actually maximised. Only
                          rendered when meaningfully different from the
                          raw total (≥0.5 FP delta) AND non-null;
                          NBA/NFL/CBB lineups ship total_adjusted_fp=null
                          and this row stays clean. */}
                      {lineup.total_adjusted_fp != null
                        && Math.abs(lineup.total_adjusted_fp - lineup.total_projected_fp) >= 0.5 && (
                        <div
                          className="text-[10px] font-bold text-orange-300"
                          title={
                            `Raw: ${lineup.total_projected_fp.toFixed(1)}  →  ` +
                            `Adjusted: ${lineup.total_adjusted_fp.toFixed(1)}\n` +
                            `Park/Wind-adjusted total used by the optimizer.`
                          }
                        >
                          Adj {lineup.total_adjusted_fp.toFixed(1)}
                        </div>
                      )}
                      {baselineScore > 0 && (
                        <div className="text-[9px] font-normal text-ticker-muted">
                          {((lineup.total_projected_fp / baselineScore) * 100).toFixed(0)}%
                        </div>
                      )}
                    </div>
                  </td>
                  <td className="px-2 py-1.5 text-right text-ticker-muted font-mono">
                    {lineup.total_floor_fp.toFixed(1)}
                  </td>
                  <td className="px-2 py-1.5 text-right text-ticker-muted font-mono">
                    {lineup.total_ceiling_fp.toFixed(1)}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono text-ticker-muted">
                    {(() => {
                      const own = lineup.players.reduce((s, p) => s + (p?.estimated_ownership || 0), 0)
                      return own > 0 ? (
                        <span className={own > 150 ? 'text-red-400' : own > 100 ? 'text-yellow-400' : 'text-green-400'}>
                          {own.toFixed(0)}%
                        </span>
                      ) : '—'
                    })()}
                  </td>
                </tr>
              )
            })}

            {/* Averages footer */}
            {lineups.length > 1 && (
              <tr className="border-t-2 border-ticker-border bg-ticker-bg/40 font-semibold">
                <td className="px-2 py-2 text-ticker-muted sticky left-0 bg-ticker-bg/40">
                  AVG
                </td>
                {slots.map((slot) => (
                  <td key={`avg-${slot}`} className="px-1.5 py-2" />
                ))}
                <td className="px-2 py-2 text-right font-mono text-white">
                  ${(avgSalary / 1000).toFixed(1)}K
                </td>
                <td
                  className={`px-2 py-2 text-right font-bold ${
                    isDK ? 'text-green-400' : 'text-blue-400'
                  }`}
                >
                  {avgFP}
                </td>
                <td className="px-2 py-2 text-right text-ticker-muted font-mono">
                  {avgFloor}
                </td>
                <td className="px-2 py-2 text-right text-ticker-muted font-mono">
                  {avgCeil}
                </td>
                <td className="px-2 py-2 text-right text-ticker-muted font-mono">
                  {(() => {
                    const avg = lineups.reduce((s, l) => 
                      s + l.players.reduce((ps, p) => ps + (p?.estimated_ownership || 0), 0)
                    , 0) / lineups.length
                    return avg > 0 ? `${avg.toFixed(0)}%` : '—'
                  })()}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination controls */}
      {needsPagination && (
        <div className="px-4 py-2 border-t border-ticker-border flex items-center justify-between">
          <span className="text-xs text-ticker-muted">
            Showing {pageStart + 1}â€“{pageEnd} of {lineups.length}
          </span>
          <div className="flex items-center gap-1.5">
            {/* Jump to selected page if not visible */}
            {page !== selectedPage && (
              <button
                onClick={goToSelectedPage}
                className="px-2 py-1 text-[10px] font-semibold rounded bg-ticker-bg hover:bg-ticker-border text-ticker-muted hover:text-white transition-colors"
                title={`Go to selected lineup (#${selectedIndex + 1})`}
              >
                #{selectedIndex + 1}
              </button>
            )}
            <button
              onClick={() => handlePageChange(page - 1)}
              disabled={page === 0}
              className="p-1 rounded hover:bg-ticker-bg transition-colors disabled:opacity-30"
            >
              <ChevronLeft className="w-3.5 h-3.5" />
            </button>
            {/* Page numbers */}
            {Array.from({ length: Math.min(totalPages, 7) }, (_, i) => {
              let pageNum
              if (totalPages <= 7) {
                pageNum = i
              } else if (page < 3) {
                pageNum = i
              } else if (page > totalPages - 4) {
                pageNum = totalPages - 7 + i
              } else {
                pageNum = page - 3 + i
              }
              return (
                <button
                  key={pageNum}
                  onClick={() => handlePageChange(pageNum)}
                  className={`px-2 py-1 text-[10px] font-semibold rounded transition-colors min-w-[24px] ${
                    page === pageNum
                      ? isDK
                        ? 'bg-green-600/30 text-green-400'
                        : 'bg-blue-600/30 text-blue-400'
                      : 'text-ticker-muted hover:text-white hover:bg-ticker-bg'
                  }`}
                >
                  {pageNum + 1}
                </button>
              )
            })}
            <button
              onClick={() => handlePageChange(page + 1)}
              disabled={page >= totalPages - 1}
              className="p-1 rounded hover:bg-ticker-bg transition-colors disabled:opacity-30"
            >
              <ChevronRight className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      )}
    </div>
    </>
  )
}
