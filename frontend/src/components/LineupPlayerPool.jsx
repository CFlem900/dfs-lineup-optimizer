/**
 * LineupPlayerPool — Sortable player pool table with lock/exclude toggles
 * and inline-editable projection cells.
 *
 * Columns: Lock | Excl | Player | Team | Pos | Salary | Min* | FP* | Src | Value | Own% | Floor* | Ceil*
 *
 * Features:
 * - Lock (green) / Exclude (red) toggles per player
 * - Sortable columns (click header)
 * - Position filter pills (PG / SG / SF / PF / C / ALL)
 * - Highlight players currently in the optimized lineup
 * - Click-to-edit for Min, FP, Floor, Ceil columns (amber highlight when edited)
 * - Per-player reset (undo icon) and "Reset All Edits" button
 */

import React, { useState, useMemo, useRef, useEffect } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { Lock, X, ArrowUpDown, ArrowUp, ArrowDown, Users, RotateCcw, RefreshCw, ChevronDown, ChevronRight, AlertCircle } from 'lucide-react'

// Position-filter chips: pulled from the per-sport shape lookup so adding
// a sport (NFL/MLB) doesn't require touching this file. Kept as legacy
// constants below for any code that imports them directly.
import { getPositionFilters } from '../config/sportShapes'
const NBA_POSITION_FILTERS = ['ALL', 'PG', 'SG', 'SF', 'PF', 'C']
const CBB_POSITION_FILTERS = ['ALL', 'G', 'F', 'C']

const EDITABLE_FIELDS = new Set(['projected_minutes', 'projected_fp', 'floor_fp', 'ceiling_fp'])

const COLUMNS = [
  { key: 'player_name', label: 'Player', align: 'left' },
  { key: 'team_abbreviation', label: 'Team', align: 'left' },
  { key: 'position', label: 'Pos', align: 'center' },
  { key: 'salary', label: 'Salary', align: 'right' },
  { key: 'projected_minutes', label: 'Min', align: 'right' },
  { key: 'projected_fp', label: 'FP', align: 'right' },
  { key: 'projection_source', label: 'Src', align: 'center' },
  { key: 'dk_value', label: 'Value', align: 'right' },
  { key: 'estimated_ownership', label: 'Own%', align: 'right' },
  { key: 'floor_fp', label: 'Floor', align: 'right' },
  { key: 'ceiling_fp', label: 'Ceil', align: 'right' },
]

// EnvMultiplierBadge moved to its own file in Prompt 7.4 so
// LineupGrid + LineupDetailModal can share the exact same chip
// without duplicating the visual / tooltip / threshold logic.
// See ``./EnvMultiplierBadge.jsx`` for the canonical implementation.
import EnvMultiplierBadge from './EnvMultiplierBadge'

// Small inline input for editing a numeric cell
function EditableInput({ value, onCommit, onCancel }) {
  const ref = useRef(null)

  useEffect(() => {
    if (ref.current) {
      ref.current.focus()
      ref.current.select()
    }
  }, [])

  const commit = () => {
    const v = ref.current?.value
    if (v !== undefined && v !== '') onCommit(v)
    else onCancel()
  }

  return (
    <input
      ref={ref}
      type="number"
      step="0.1"
      defaultValue={value.toFixed(1)}
      className="w-14 px-1 py-0.5 text-xs text-right bg-ticker-bg border border-amber-500/50 rounded outline-none font-mono text-amber-300"
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') { e.preventDefault(); commit() }
        if (e.key === 'Escape') { e.preventDefault(); onCancel() }
        // Prevent table sort clicks while typing
        e.stopPropagation()
      }}
      onClick={(e) => e.stopPropagation()}
    />
  )
}

const ROW_HEIGHT = 36

// Explicit column widths shared between the header table and virtualised body rows
// so that columns stay aligned across two separate <table> elements.
const COL_WIDTHS = [
  36,    // Lock
  36,    // Exclude
  null,  // Player (takes remaining space)
  56,    // Team
  50,    // Pos
  68,    // Salary
  56,    // Min
  56,    // FP
  40,    // Src (projection source)
  56,    // Value
  56,    // Own%
  56,    // Floor
  56,    // Ceil
]

function SharedColGroup() {
  return (
    <colgroup>
      {COL_WIDTHS.map((w, i) => (
        <col key={i} style={w ? { width: w } : undefined} />
      ))}
    </colgroup>
  )
}

// CSS grid template for virtualised body rows (must match COL_WIDTHS)
const GRID_TEMPLATE = COL_WIDTHS.map((w) => (w ? `${w}px` : '1fr')).join(' ')

function VirtualizedBody({
  displayed,
  lockedPlayers,
  excludedPlayers,
  lineupPlayerIds,
  isDK,
  isPlayerEdited,
  onToggleLock,
  onToggleExclude,
  onResetPlayer,
  renderEditableCell,
}) {
  const parentRef = useRef(null)

  const virtualizer = useVirtualizer({
    count: displayed.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 5,
  })

  if (displayed.length === 0) return null

  return (
    <div
      ref={parentRef}
      className="overflow-y-auto"
      style={{ maxHeight: Math.min(displayed.length * ROW_HEIGHT, 600) }}
    >
      <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
        <table className="w-full text-sm whitespace-nowrap" style={{ tableLayout: 'fixed' }}>
          <SharedColGroup />
          <tbody>
            {virtualizer.getVirtualItems().map((virtualRow) => {
              const idx = virtualRow.index
              const player = displayed[idx]
              const isLocked = lockedPlayers.has(player.player_id)
              const isExcluded = excludedPlayers.has(player.player_id)
              const inLineup = lineupPlayerIds.has(player.player_id)
              const playerEdited = isPlayerEdited(player.player_id)

              return (
                <tr
                  key={`${player.player_id}-${player.position}-${idx}`}
                  style={{
                    height: ROW_HEIGHT,
                    position: 'absolute',
                    top: 0,
                    left: 0,
                    width: '100%',
                    transform: `translateY(${virtualRow.start}px)`,
                    display: 'grid',
                    gridTemplateColumns: GRID_TEMPLATE,
                    alignItems: 'center',
                  }}
                  className={`border-b border-ticker-border/50 transition-colors ${
                    isExcluded
                      ? 'opacity-40 line-through'
                      : inLineup
                      ? (isDK ? 'bg-green-900/10' : 'bg-blue-900/10')
                      : 'hover:bg-ticker-bg/30'
                  } ${isLocked ? (isDK ? 'border-l-2 border-l-green-500' : 'border-l-2 border-l-blue-500') : ''}`}
                >
                  {/* Lock Toggle */}
                  <td className="w-8 px-2 py-1 text-center">
                    <button
                      onClick={() => onToggleLock?.(player.player_id)}
                      className={`w-5 h-5 inline-flex items-center justify-center rounded transition-colors ${
                        isLocked
                          ? 'bg-green-600 text-white'
                          : 'text-ticker-muted hover:text-green-400 hover:bg-green-900/20'
                      }`}
                      title={isLocked ? 'Unlock' : 'Lock'}
                    >
                      <Lock className="w-3 h-3" />
                    </button>
                  </td>

                  {/* Exclude Toggle */}
                  <td className="w-8 px-2 py-1 text-center">
                    <button
                      onClick={() => onToggleExclude?.(player.player_id)}
                      className={`w-5 h-5 inline-flex items-center justify-center rounded transition-colors ${
                        isExcluded
                          ? 'bg-red-600 text-white'
                          : 'text-ticker-muted hover:text-red-400 hover:bg-red-900/20'
                      }`}
                      title={isExcluded ? 'Include' : 'Exclude'}
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </td>

                  {/* Player Name + Injury Badge + Reset icon */}
                  <td className="px-2 py-1 overflow-hidden min-w-0">
                    <span className="inline-flex items-center gap-1 max-w-full">
                      <span className="font-semibold text-white truncate">{player.player_name}</span>
                      {player.projection_source && (
                        <span
                          className="shrink-0 px-1 py-0.5 text-[9px] font-bold rounded leading-none bg-amber-900/40 text-amber-400"
                          title={`Approximate projection (${player.projection_source})`}
                        >
                          ~EST
                        </span>
                      )}
                      {player.injury_status && (
                        <span
                          className={`shrink-0 px-1 py-0.5 text-[9px] font-bold rounded leading-none ${
                            player.injury_status === 'Out'
                              ? 'bg-red-900/40 text-red-400'
                              : player.injury_status === 'Doubtful'
                              ? 'bg-orange-900/40 text-orange-400'
                              : player.injury_status === 'GTD' || player.injury_status === 'Game Time Decision'
                              ? 'bg-yellow-900/40 text-yellow-400'
                              : player.injury_status === 'Questionable'
                              ? 'bg-blue-900/40 text-blue-400'
                              : 'bg-gray-800 text-gray-400'
                          }`}
                          title={player.injury_description || player.injury_status}
                        >
                          {player.injury_status === 'Game Time Decision' ? 'GTD' : player.injury_status}
                        </span>
                      )}
                      {playerEdited && (
                        <button
                          onClick={(e) => { e.stopPropagation(); onResetPlayer?.(player.player_id) }}
                          className="shrink-0 text-amber-400 hover:text-amber-300 transition-colors"
                          title="Reset this player to original projections"
                        >
                          <RotateCcw className="w-3 h-3" />
                        </button>
                      )}
                    </span>
                  </td>

                  {/* Team + Game Context */}
                  <td className="px-2 py-1 text-ticker-muted">
                    <span className="inline-flex items-center gap-0.5">
                      {player.team_abbreviation || <span className="text-ticker-muted/40">&mdash;</span>}
                      {player.game_total > 0 && (
                        <span className="text-[9px] text-ticker-muted/60 ml-0.5" title={`Over/Under: ${player.game_total.toFixed(1)}`}>
                          {player.game_total.toFixed(0)}
                        </span>
                      )}
                    </span>
                  </td>

                  {/* Position */}
                  <td className="px-2 py-1 text-center">
                    <span className="px-1.5 py-0.5 bg-ticker-bg rounded text-xs">{player.position}</span>
                  </td>

                  {/* Salary */}
                  <td className="px-2 py-1 text-right font-mono text-white">
                    ${(player.salary / 1000).toFixed(1)}K
                  </td>

                  {/* Minutes — Editable */}
                  <td className="px-2 py-1 text-right font-mono">
                    {renderEditableCell(
                      player,
                      'projected_minutes',
                      (player.projected_minutes ?? 0).toFixed(1),
                      'text-ticker-muted'
                    )}
                  </td>

                  {/* FP — Editable, with env-multiplier badge for MLB
                      park/wind adjustments (Prompt 7.2). The badge
                      sits to the right of the FP value and only
                      renders when ``env_multiplier !== 1`` — so non-
                      MLB sports never see it. */}
                  <td className="px-2 py-1 text-right">
                    <div className="flex items-center justify-end gap-1">
                      {renderEditableCell(
                        player,
                        'projected_fp',
                        (player.projected_fp ?? 0).toFixed(1),
                        `font-bold ${isDK ? 'text-green-400' : 'text-blue-400'}`
                      )}
                      <EnvMultiplierBadge player={player} />
                    </div>
                  </td>

                  {/* Projection Source Badge */}
                  <td className="px-1 py-1 text-center">
                    {(() => {
                      const src = player.projection_source
                      if (src === 'rotation') return (
                        <span className="inline-block px-1 py-0.5 text-[9px] font-bold rounded leading-none bg-green-900/40 text-green-400" title="Rotation engine projection (high confidence)">R</span>
                      )
                      if (src === 'dk_fppg') return (
                        <span className="inline-block px-1 py-0.5 text-[9px] font-bold rounded leading-none bg-yellow-900/40 text-yellow-400" title="DraftKings FPPG average (medium confidence)">DK</span>
                      )
                      if (src === 'salary_estimate') return (
                        <span className="inline-block px-1 py-0.5 text-[9px] font-bold rounded leading-none bg-red-900/40 text-red-400" title="Salary-based estimate (low confidence)">S</span>
                      )
                      return (
                        <span className="inline-block px-1 py-0.5 text-[9px] font-bold rounded leading-none bg-gray-800 text-gray-500" title="Unknown projection source">?</span>
                      )
                    })()}
                  </td>

                  {/* Value */}
                  <td className="px-2 py-1 text-right font-mono text-ticker-muted">
                    {(player.dk_value || 0).toFixed(1)}x
                  </td>

                  {/* Ownership */}
                  <td className="px-2 py-1 text-right font-mono">
                    {player.estimated_ownership != null ? (
                      <span className={`${
                        player.estimated_ownership >= 30
                          ? 'text-red-400'
                          : player.estimated_ownership >= 15
                          ? 'text-yellow-400'
                          : 'text-ticker-muted'
                      }`}>
                        {player.estimated_ownership.toFixed(1)}%
                      </span>
                    ) : (
                      <span className="text-ticker-muted/50">&mdash;</span>
                    )}
                  </td>

                  {/* Floor — Editable */}
                  <td className="px-2 py-1 text-right font-mono">
                    {renderEditableCell(
                      player,
                      'floor_fp',
                      (player.floor_fp ?? 0).toFixed(1),
                      'text-ticker-muted'
                    )}
                  </td>

                  {/* Ceiling — Editable */}
                  <td className="px-2 py-1 text-right font-mono">
                    {renderEditableCell(
                      player,
                      'ceiling_fp',
                      (player.ceiling_fp ?? 0).toFixed(1),
                      'text-ticker-muted'
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Exclusion reason badge ────────────────────────────────────────
const EXCLUSION_STYLES = {
  injury_out:      { label: 'OUT',       bg: 'bg-red-900/40',    text: 'text-red-400' },
  injury_doubtful: { label: 'DOUBTFUL',  bg: 'bg-orange-900/40', text: 'text-orange-400' },
  zero_minutes:    { label: '0 MIN',     bg: 'bg-gray-800',      text: 'text-gray-400' },
  zero_fp:         { label: '0 FP',      bg: 'bg-gray-800',      text: 'text-gray-400' },
  low_games:       { label: 'G-LEAGUE',  bg: 'bg-purple-900/40', text: 'text-purple-400' },
  name_mismatch:   { label: 'UNMATCHED', bg: 'bg-yellow-900/40', text: 'text-yellow-400' },
}

function ExclusionBadge({ reason }) {
  const style = EXCLUSION_STYLES[reason] || { label: reason, bg: 'bg-gray-800', text: 'text-gray-400' }
  return (
    <span className={`px-1.5 py-0.5 text-[9px] font-bold rounded leading-none ${style.bg} ${style.text}`}>
      {style.label}
    </span>
  )
}

function ExcludedPlayersSection({ excludedPool = [] }) {
  const [isOpen, setIsOpen] = useState(false)

  if (excludedPool.length === 0) return null

  // Group by reason for a compact summary
  const reasonCounts = {}
  for (const p of excludedPool) {
    const label = EXCLUSION_STYLES[p.exclusion_reason]?.label || p.exclusion_reason
    reasonCounts[label] = (reasonCounts[label] || 0) + 1
  }

  return (
    <div className="border-t border-ticker-border">
      <button
        onClick={() => setIsOpen((v) => !v)}
        className="w-full px-4 py-2 flex items-center gap-2 text-xs text-ticker-muted hover:text-white hover:bg-ticker-bg/30 transition-colors"
      >
        {isOpen ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        <AlertCircle className="w-3.5 h-3.5 text-ticker-muted" />
        <span className="font-semibold uppercase tracking-wider">
          Excluded Players ({excludedPool.length})
        </span>
        <span className="text-[10px] text-ticker-muted">
          {Object.entries(reasonCounts).map(([label, cnt]) => `${cnt} ${label}`).join(' \u00B7 ')}
        </span>
      </button>

      {isOpen && (
        <div className="overflow-y-auto max-h-[200px]">
          <table className="w-full text-sm whitespace-nowrap">
            <thead>
              <tr className="text-[10px] text-ticker-muted uppercase tracking-wider border-b border-ticker-border/50">
                <th className="px-3 py-1.5 text-left">Player</th>
                <th className="px-2 py-1.5 text-left w-14">Team</th>
                <th className="px-2 py-1.5 text-center w-12">Pos</th>
                <th className="px-2 py-1.5 text-right w-16">Salary</th>
                <th className="px-2 py-1.5 text-left w-24">Reason</th>
              </tr>
            </thead>
            <tbody>
              {excludedPool.map((player, idx) => (
                <tr
                  key={`excl-${player.player_id}-${idx}`}
                  className="border-b border-ticker-border/20 opacity-40"
                >
                  <td className="px-3 py-1 text-xs">
                    <span className="line-through">{player.player_name}</span>
                  </td>
                  <td className="px-2 py-1">
                    <span className="px-1.5 py-0.5 bg-ticker-bg rounded text-[10px] font-semibold text-ticker-muted">
                      {player.team_abbreviation}
                    </span>
                  </td>
                  <td className="px-2 py-1 text-center">
                    <span className="px-1.5 py-0.5 bg-ticker-bg rounded text-[10px]">{player.position}</span>
                  </td>
                  <td className="px-2 py-1 text-right text-xs font-mono text-ticker-muted">
                    {player.salary > 0 ? `$${(player.salary / 1000).toFixed(1)}K` : '\u2014'}
                  </td>
                  <td className="px-2 py-1">
                    <ExclusionBadge reason={player.exclusion_reason} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default function LineupPlayerPool({
  pool = [],
  excludedPool = [],
  lockedPlayers = new Set(),
  excludedPlayers = new Set(),
  lineupPlayerIds = new Set(),
  platform = 'dk',
  sport = 'nba',
  onToggleLock,
  onToggleExclude,
  onProjectionEdit,
  projectionOverrides = {},
  onResetPlayer,
  onResetAll,
  hasEdits = false,
  onRefreshPool,
}) {
  const [sortKey, setSortKey] = useState('projected_fp')
  const [sortDir, setSortDir] = useState('desc')
  const [posFilter, setPosFilter] = useState('ALL')
  const [editingCell, setEditingCell] = useState(null) // { playerId, field }

  const isDK = platform === 'dk'
  const isCBB = sport === 'cbb'
  // Sport-aware position filters: NBA → PG/SG/SF/PF/C; CBB → G/F/C;
  // NFL → QB/RB/WR/TE/DST; MLB → P/C/1B/2B/3B/SS/OF. Pulled from the
  // central sport-shapes lookup so adding a sport is one registry edit.
  const POSITION_FILTERS = getPositionFilters(sport)

  // Reset filter when switching sports (avoid stale filter like "PG" in CBB)
  useEffect(() => {
    setPosFilter('ALL')
  }, [sport])

  // Sort + filter
  const displayed = useMemo(() => {
    let filtered = pool
    if (posFilter !== 'ALL') {
      // For CBB, support dual-position matching (e.g., "G/F" matches "G" or "F")
      filtered = filtered.filter((p) => {
        if (p.position === posFilter) return true
        if (p.position.includes('/')) {
          return p.position.split('/').some((part) => part.trim() === posFilter)
        }
        return false
      })
    }
    const sorted = [...filtered].sort((a, b) => {
      let aVal = a[sortKey]
      let bVal = b[sortKey]
      if (typeof aVal === 'string') aVal = aVal.toLowerCase()
      if (typeof bVal === 'string') bVal = bVal.toLowerCase()
      if (aVal == null) aVal = -Infinity
      if (bVal == null) bVal = -Infinity
      if (aVal < bVal) return sortDir === 'asc' ? -1 : 1
      if (aVal > bVal) return sortDir === 'asc' ? 1 : -1
      return 0
    })
    return sorted
  }, [pool, sortKey, sortDir, posFilter])

  const handleSort = (key) => {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      setSortDir(key === 'player_name' || key === 'team_abbreviation' || key === 'position' ? 'asc' : 'desc')
    }
  }

  const SortIcon = ({ colKey }) => {
    if (sortKey !== colKey) return <ArrowUpDown className="w-3 h-3 opacity-30" />
    return sortDir === 'asc'
      ? <ArrowUp className="w-3 h-3 text-ticker-green" />
      : <ArrowDown className="w-3 h-3 text-ticker-green" />
  }

  // Check if a specific cell is edited
  const isFieldEdited = (playerId, field) => {
    return projectionOverrides?.[playerId]?.[field] !== undefined
  }

  // Check if any field on a player is edited
  const isPlayerEdited = (playerId) => {
    return projectionOverrides?.[playerId] && Object.keys(projectionOverrides[playerId]).length > 0
  }

  // Render an editable or display cell
  const renderEditableCell = (player, field, displayValue, defaultClass) => {
    const isEditing = editingCell?.playerId === player.player_id && editingCell?.field === field
    const isEdited = isFieldEdited(player.player_id, field)

    if (isEditing) {
      return (
        <EditableInput
          value={player[field]}
          onCommit={(val) => {
            onProjectionEdit?.(player.player_id, field, val)
            setEditingCell(null)
          }}
          onCancel={() => setEditingCell(null)}
        />
      )
    }

    return (
      <span
        onClick={(e) => {
          e.stopPropagation()
          setEditingCell({ playerId: player.player_id, field })
        }}
        className={`cursor-text hover:bg-amber-500/10 hover:border-b hover:border-amber-500/30 px-0.5 rounded transition-colors ${
          isEdited ? 'text-amber-400 font-bold border-b border-amber-500/30' : defaultClass
        }`}
        title={isEdited ? `Edited (click to change)` : 'Click to edit'}
      >
        {displayValue}
      </span>
    )
  }

  return (
    <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-ticker-border flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Users className="w-4 h-4 text-ticker-green" />
          <h3 className="text-sm font-semibold uppercase tracking-wider">Player Pool</h3>
          <span className="px-2 py-0.5 bg-ticker-green/10 text-ticker-green text-xs font-bold rounded-full">
            {pool.length}
          </span>
          {hasEdits && (
            <span className="px-2 py-0.5 bg-amber-500/10 text-amber-400 text-xs font-bold rounded-full">
              Edited
            </span>
          )}
          {/* Refresh Pool button — clears all caches, re-fetches from NBA API */}
          {onRefreshPool && (
            <button
              onClick={onRefreshPool}
              className="p-1 text-ticker-muted hover:text-ticker-green transition-colors"
              title="Refresh player pool from live data (clears cache)"
            >
              <RefreshCw className="w-3.5 h-3.5" />
            </button>
          )}
        </div>

        <div className="flex items-center gap-2">
          {/* Reset All button */}
          {hasEdits && (
            <button
              onClick={onResetAll}
              className="flex items-center gap-1 px-2.5 py-1 text-xs font-semibold text-amber-400 bg-amber-500/10 hover:bg-amber-500/20 rounded transition-colors"
              title="Reset all projection edits to original values"
            >
              <RotateCcw className="w-3 h-3" />
              Reset All
            </button>
          )}

          {/* Position Filter Pills */}
          {POSITION_FILTERS.map((pos) => (
            <button
              key={pos}
              onClick={() => setPosFilter(pos)}
              className={`px-2.5 py-1 text-xs font-semibold rounded transition-colors ${
                posFilter === pos
                  ? (isDK ? 'bg-green-600 text-white' : 'bg-blue-600 text-white')
                  : 'text-ticker-muted hover:text-white hover:bg-ticker-bg'
              }`}
            >
              {pos}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm whitespace-nowrap" style={{ tableLayout: 'fixed' }}>
          <SharedColGroup />
          <thead>
            <tr className="text-xs text-ticker-muted uppercase tracking-wider border-b border-ticker-border">
              {/* Lock / Excl */}
              <th className="w-8 px-2 py-2 text-center" title="Lock">
                <Lock className="w-3 h-3 mx-auto opacity-50" />
              </th>
              <th className="w-8 px-2 py-2 text-center" title="Exclude">
                <X className="w-3 h-3 mx-auto opacity-50" />
              </th>
              {COLUMNS.map((col) => (
                <th
                  key={col.key}
                  className={`px-2 py-2 cursor-pointer select-none hover:text-white transition-colors ${
                    col.align === 'right' ? 'text-right' : col.align === 'center' ? 'text-center' : 'text-left'
                  }`}
                  onClick={() => handleSort(col.key)}
                >
                  <span className="inline-flex items-center gap-1">
                    {col.label}
                    {EDITABLE_FIELDS.has(col.key) && <span className="text-amber-500/40 text-[8px]" title="Editable">*</span>}
                    <SortIcon colKey={col.key} />
                  </span>
                </th>
              ))}
            </tr>
          </thead>
        </table>

        <VirtualizedBody
          displayed={displayed}
          lockedPlayers={lockedPlayers}
          excludedPlayers={excludedPlayers}
          lineupPlayerIds={lineupPlayerIds}
          isDK={isDK}
          isPlayerEdited={isPlayerEdited}
          onToggleLock={onToggleLock}
          onToggleExclude={onToggleExclude}
          onResetPlayer={onResetPlayer}
          renderEditableCell={renderEditableCell}
        />

        {displayed.length === 0 && (
          <div className="py-8 text-center text-sm text-ticker-muted">
            {pool.length === 0 ? 'No players loaded — select a slate first.' : 'No players match the current filter.'}
          </div>
        )}
      </div>

      {/* Excluded Players — collapsible section */}
      <ExcludedPlayersSection excludedPool={excludedPool} />
    </div>
  )
}
