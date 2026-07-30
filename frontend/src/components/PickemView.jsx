/**
 * PickemView — Main Underdog pick'em interface.
 *
 * Two-column layout:
 *   Left  (2/3): Pick cards grid with filters + search
 *   Right (1/3): Entry builder sidebar with payout table
 */

import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { RefreshCw, Zap, Target, Trash2, Sparkles, AlertTriangle, Search, TrendingUp, TrendingDown, Info } from 'lucide-react'
import { rotationAPI } from '../services/api'
import PickemCard from './PickemCard'

const NBA_STAT_FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'pts', label: 'PTS' },
  { key: 'reb', label: 'REB' },
  { key: 'ast', label: 'AST' },
  { key: 'pra', label: 'PRA' },
  { key: 'fg3m', label: '3PM' },
  { key: 'stl_blk', label: 'STL+BLK' },
  { key: 'fantasy_points', label: 'FPTS' },
]

const CBB_STAT_FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'pts', label: 'PTS' },
  { key: 'reb', label: 'REB' },
  { key: 'ast', label: 'AST' },
  { key: 'pra', label: 'PRA' },
  { key: 'fg3m', label: '3PM' },
  { key: 'fantasy_points', label: 'FPTS' },
]

const EDGE_FILTERS = [
  { key: 'all', label: 'All Edges' },
  { key: 'strong', label: 'Strong' },
  { key: 'moderate', label: 'Moderate+' },
  { key: 'slight', label: 'Slight+' },
]

const STRATEGIES = [
  { key: 'max_edge', label: 'Max Edge', desc: 'Highest delta %' },
  { key: 'diversified', label: 'Diversified', desc: 'Spread across stats' },
  { key: 'correlated', label: 'Correlated', desc: 'Same-team stacks' },
]

const EDGE_ORDER = { strong: 0, moderate: 1, slight: 2, no_edge: 3 }

const FLEX_PAYOUTS = { 2: '3x', 3: '6x', 4: '10x', 5: '20x', 6: '40x' }
const POWER_PAYOUTS = { 2: '6x', 3: '10x', 4: '25x', 5: '50x', 6: '100x' }

export default function PickemView({ selectedDate, sport = 'nba' }) {
  // ── State ──────────────────────────────────────────────────────────
  const [edges, setEdges] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  // Filters
  const [statFilter, setStatFilter] = useState('all')
  const [edgeFilter, setEdgeFilter] = useState('all')
  const [searchQuery, setSearchQuery] = useState('')

  // Entry builder
  const [selectedPicks, setSelectedPicks] = useState([])
  const [entryType, setEntryType] = useState('flex')
  const [strategy, setStrategy] = useState('max_edge')
  const [numPicks, setNumPicks] = useState(5)
  const [buildingEntry, setBuildingEntry] = useState(false)

  // Sport-aware stat filters
  const STAT_FILTERS = sport === 'cbb' ? CBB_STAT_FILTERS : NBA_STAT_FILTERS

  // Reset stat filter when sport changes if current filter doesn't exist
  useEffect(() => {
    if (!STAT_FILTERS.some(f => f.key === statFilter)) {
      setStatFilter('all')
    }
  }, [sport]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Fetch edges ────────────────────────────────────────────────────
  const fetchEdges = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await rotationAPI.getUnderdogEdges({
        gameDate: selectedDate,
        sport,
      })
      setEdges(data)
    } catch (err) {
      setError(err.message || 'Failed to load Underdog data')
    } finally {
      setLoading(false)
    }
  }, [selectedDate, sport])

  useEffect(() => {
    fetchEdges()
  }, [fetchEdges])

  // ── Filtered & sorted lines ───────────────────────────────────────
  const filteredLines = useMemo(() => {
    if (!edges?.lines) return []
    let lines = edges.lines

    // Stat filter
    if (statFilter !== 'all') {
      lines = lines.filter(l => l.stat === statFilter)
    }

    // Edge strength filter
    if (edgeFilter === 'strong') {
      lines = lines.filter(l => l.edge_strength === 'strong')
    } else if (edgeFilter === 'moderate') {
      lines = lines.filter(l => l.edge_strength === 'strong' || l.edge_strength === 'moderate')
    } else if (edgeFilter === 'slight') {
      lines = lines.filter(l => l.edge_strength !== 'no_edge')
    }

    // Player name search
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase().trim()
      lines = lines.filter(l =>
        l.player_name.toLowerCase().includes(q) ||
        (l.team_abbreviation || '').toLowerCase().includes(q)
      )
    }

    // Default sort: edge_strength (strong first), then by |delta_pct| descending
    lines = [...lines].sort((a, b) => {
      const edgeA = EDGE_ORDER[a.edge_strength] ?? 3
      const edgeB = EDGE_ORDER[b.edge_strength] ?? 3
      if (edgeA !== edgeB) return edgeA - edgeB
      return Math.abs(b.delta_pct || 0) - Math.abs(a.delta_pct || 0)
    })

    return lines
  }, [edges, statFilter, edgeFilter, searchQuery])

  // ── Pick selection ─────────────────────────────────────────────────
  const togglePick = useCallback((pick) => {
    setSelectedPicks(prev => {
      const key = `${pick.player_name}:${pick.stat}`
      const exists = prev.some(p => `${p.player_name}:${p.stat}` === key)
      if (exists) {
        return prev.filter(p => `${p.player_name}:${p.stat}` !== key)
      }
      if (prev.length >= 6) return prev // Max 6 picks
      return [...prev, pick]
    })
  }, [])

  const isSelected = useCallback((pick) => {
    return selectedPicks.some(
      p => p.player_name === pick.player_name && p.stat === pick.stat
    )
  }, [selectedPicks])

  // ── Build optimal entry ────────────────────────────────────────────
  const handleBuildOptimal = useCallback(async () => {
    setBuildingEntry(true)
    setError(null)
    try {
      const data = await rotationAPI.buildPickemEntry({
        numPicks,
        strategy,
        entryType,
        gameDate: selectedDate,
        sport,
      })
      if (data.picks) {
        setSelectedPicks(data.picks)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setBuildingEntry(false)
    }
  }, [numPicks, strategy, entryType, selectedDate, sport])

  // ── Payout calculator ──────────────────────────────────────────────
  const payoutMultiplier = useMemo(() => {
    const n = selectedPicks.length
    if (n < 2) return 0
    const table = entryType === 'power_play'
      ? { 2: 6, 3: 10, 4: 25, 5: 50, 6: 100 }
      : { 2: 3, 3: 6, 4: 10, 5: 20, 6: 40 }
    return table[n] || 0
  }, [selectedPicks.length, entryType])

  // Pool required check
  const noPool = edges && edges.total_lines === 0

  // Detect broken data quality (UUIDs, "???" in player names/teams)
  const hasBrokenData = !loading && edges?.lines?.some(l =>
    !l.player_name || l.player_name === '???' ||
    /^[0-9a-f]{8}-/i.test(l.player_name) ||
    l.team_abbreviation === '???'
  )

  // ── Render ─────────────────────────────────────────────────────────
  return (
    <div className="flex gap-4 h-full">
      {/* ── LEFT: Pick Cards Grid ─────────────────────────────────── */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Header */}
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-3">
            <h2 className="text-sm font-semibold text-white flex items-center gap-2">
              <Target className="w-4 h-4 text-ticker-green" />
              Pick'em Edge Finder
            </h2>
            {edges && !noPool && (
              <div className="flex items-center gap-2 text-xs">
                <span className="text-ticker-muted">{edges.total_lines} lines</span>
                {edges.strong_edges > 0 && (
                  <span className="text-ticker-green font-medium">{edges.strong_edges} strong</span>
                )}
                {edges.moderate_edges > 0 && (
                  <span className="text-yellow-400">{edges.moderate_edges} moderate</span>
                )}
              </div>
            )}
          </div>
          <button
            onClick={fetchEdges}
            disabled={loading}
            className="flex items-center gap-1 text-xs text-ticker-muted hover:text-white transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>

        {/* Pool required prompt */}
        {noPool && !loading && (
          <div className="flex items-center gap-3 p-4 mb-3 bg-yellow-500/10 border border-yellow-500/30 rounded-lg">
            <Info className="w-5 h-5 text-yellow-400 flex-shrink-0" />
            <div>
              <p className="text-sm font-medium text-yellow-300">Player Pool Required</p>
              <p className="text-xs text-yellow-400/70 mt-0.5">
                Build a player pool first via the <span className="font-semibold text-yellow-300">Lineup</span> tab.
                Underdog edges are calculated by comparing your projections to UD lines.
              </p>
            </div>
          </div>
        )}

        {/* Data quality warning */}
        {hasBrokenData && !noPool && (
          <div className="flex items-center gap-3 p-4 mb-3 bg-orange-500/10 border border-orange-500/30 rounded-lg">
            <AlertTriangle className="w-5 h-5 text-orange-400 flex-shrink-0" />
            <div>
              <p className="text-sm font-medium text-orange-300">Data Quality Issue</p>
              <p className="text-xs text-orange-400/70 mt-0.5">
                Some player names or teams are not resolving correctly.
                The Underdog API format may have changed. Check server logs for details.
              </p>
            </div>
          </div>
        )}

        {/* Filter bar */}
        {!noPool && (
          <div className="flex items-center gap-3 mb-3 flex-wrap">
            {/* Player search */}
            <div className="relative">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3 h-3 text-ticker-muted" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search player..."
                className="pl-7 pr-2 py-1 text-[11px] w-36 bg-ticker-bg border border-ticker-border rounded-md text-white placeholder-ticker-muted/50 focus:outline-none focus:border-ticker-green/50"
              />
            </div>

            {/* Stat filter */}
            <div className="flex items-center gap-1 bg-ticker-bg rounded-md p-0.5 border border-ticker-border">
              {STAT_FILTERS.map(f => (
                <button
                  key={f.key}
                  onClick={() => setStatFilter(f.key)}
                  className={`px-2 py-1 text-[10px] font-semibold rounded transition-colors ${
                    statFilter === f.key
                      ? 'bg-ticker-green text-white'
                      : 'text-ticker-muted hover:text-white'
                  }`}
                >
                  {f.label}
                </button>
              ))}
            </div>

            {/* Edge strength filter */}
            <div className="flex items-center gap-1 bg-ticker-bg rounded-md p-0.5 border border-ticker-border">
              {EDGE_FILTERS.map(f => (
                <button
                  key={f.key}
                  onClick={() => setEdgeFilter(f.key)}
                  className={`px-2 py-1 text-[10px] font-semibold rounded transition-colors ${
                    edgeFilter === f.key
                      ? 'bg-ticker-green text-white'
                      : 'text-ticker-muted hover:text-white'
                  }`}
                >
                  {f.label}
                </button>
              ))}
            </div>

            <span className="text-[10px] text-ticker-muted">
              {filteredLines.length} shown
            </span>
          </div>
        )}

        {/* Content: Loading / Error / Cards Grid */}
        <div className="flex-1 overflow-y-auto">
          {/* Loading state — shown on both initial and re-fetch */}
          {loading && (
            <div className="flex items-center justify-center py-12">
              <RefreshCw className="w-6 h-6 text-ticker-green animate-spin" />
              <span className="ml-2 text-sm text-ticker-muted">Loading Underdog lines...</span>
            </div>
          )}

          {error && !loading && (
            <div className="flex items-center gap-2 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-sm text-red-400">
              <AlertTriangle className="w-4 h-4 flex-shrink-0" />
              <span>{error}</span>
              <button
                onClick={fetchEdges}
                className="ml-auto text-xs text-red-300 hover:text-white underline"
              >
                Retry
              </button>
            </div>
          )}

          {!loading && !error && edges && !noPool && filteredLines.length === 0 && (
            <div className="text-center py-12">
              <Target className="w-8 h-8 text-ticker-muted mx-auto mb-2" />
              <p className="text-sm text-ticker-muted">
                No lines match your filters.
              </p>
            </div>
          )}

          {!loading && filteredLines.length > 0 && (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-2">
              {filteredLines.map((pick) => (
                <PickemCard
                  key={`${pick.player_name}:${pick.stat}`}
                  pick={pick}
                  isSelected={isSelected(pick)}
                  onToggle={togglePick}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ── RIGHT: Entry Builder Sidebar ──────────────────────────── */}
      <div className="w-72 flex-shrink-0 flex flex-col bg-ticker-card border border-ticker-border rounded-lg overflow-hidden">
        {/* Sidebar header */}
        <div className="px-3 py-2 border-b border-ticker-border">
          <h3 className="text-sm font-semibold text-white flex items-center gap-2">
            <Zap className="w-4 h-4 text-yellow-400" />
            Entry Builder
          </h3>
        </div>

        {/* Entry type toggle */}
        <div className="px-3 py-2 border-b border-ticker-border flex items-center gap-2">
          <button
            onClick={() => setEntryType('flex')}
            className={`flex-1 px-2 py-1 text-xs font-semibold rounded transition-colors ${
              entryType === 'flex'
                ? 'bg-ticker-green text-white'
                : 'bg-ticker-bg text-ticker-muted hover:text-white'
            }`}
          >
            Flex
          </button>
          <button
            onClick={() => setEntryType('power_play')}
            className={`flex-1 px-2 py-1 text-xs font-semibold rounded transition-colors ${
              entryType === 'power_play'
                ? 'bg-purple-600 text-white'
                : 'bg-ticker-bg text-ticker-muted hover:text-white'
            }`}
          >
            Power Play
          </button>
        </div>

        {/* Payout reference table */}
        <div className="px-3 py-1.5 border-b border-ticker-border/50 bg-ticker-bg/30">
          <div className="flex items-center gap-3 text-[10px]">
            {[2, 3, 4, 5, 6].map(n => {
              const payout = entryType === 'power_play' ? POWER_PAYOUTS[n] : FLEX_PAYOUTS[n]
              const isActive = selectedPicks.length === n
              return (
                <span
                  key={n}
                  className={`font-medium ${
                    isActive
                      ? entryType === 'power_play' ? 'text-purple-400' : 'text-ticker-green'
                      : 'text-ticker-muted/60'
                  }`}
                >
                  {n}pk={payout}
                </span>
              )
            })}
          </div>
        </div>

        {/* Selected picks list */}
        <div className="flex-1 overflow-y-auto px-3 py-2">
          {selectedPicks.length === 0 ? (
            <p className="text-xs text-ticker-muted text-center py-4">
              Click cards to add picks (2-6 required)
            </p>
          ) : (
            <div className="space-y-1.5">
              {selectedPicks.map((pick, idx) => {
                const isOver = pick.recommendation === 'OVER'
                const isUnder = pick.recommendation === 'UNDER'
                return (
                  <div
                    key={`${pick.player_name}:${pick.stat}`}
                    className={`flex items-center justify-between rounded px-2 py-1.5 ${
                      isOver
                        ? 'bg-ticker-green/10 border border-ticker-green/20'
                        : isUnder
                          ? 'bg-red-500/10 border border-red-500/20'
                          : 'bg-ticker-bg border border-ticker-border/30'
                    }`}
                  >
                    <div className="min-w-0">
                      <div className="text-xs font-medium text-white truncate flex items-center gap-1">
                        {isOver && <TrendingUp className="w-3 h-3 text-ticker-green flex-shrink-0" />}
                        {isUnder && <TrendingDown className="w-3 h-3 text-ticker-red flex-shrink-0" />}
                        {pick.player_name}
                      </div>
                      <div className="flex items-center gap-1.5 text-[10px]">
                        <span className={isOver ? 'text-ticker-green font-semibold' : isUnder ? 'text-ticker-red font-semibold' : 'text-ticker-muted'}>
                          {pick.recommendation}
                        </span>
                        <span className="text-ticker-muted">
                          {pick.ud_line} {(pick.stat || '').toUpperCase()}
                        </span>
                        <span className="text-ticker-muted">
                          ({pick.delta_pct > 0 ? '+' : ''}{pick.delta_pct}%)
                        </span>
                      </div>
                    </div>
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        togglePick(pick)
                      }}
                      className="text-ticker-muted hover:text-ticker-red ml-2 flex-shrink-0"
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* Payout + actions */}
        <div className="px-3 py-2 border-t border-ticker-border space-y-2">
          {/* Payout display */}
          {selectedPicks.length >= 2 && (
            <div className="flex items-center justify-between text-xs">
              <span className="text-ticker-muted">Payout:</span>
              <span className={`font-bold text-lg ${
                entryType === 'power_play' ? 'text-purple-400' : 'text-ticker-green'
              }`}>
                {payoutMultiplier}x
              </span>
            </div>
          )}

          {/* Pick count with validation */}
          <div className="flex items-center justify-between text-xs">
            <span className="text-ticker-muted">Picks:</span>
            <span className={`font-medium ${
              selectedPicks.length >= 2 ? 'text-white' : 'text-yellow-400'
            }`}>
              {selectedPicks.length} / 6
              {selectedPicks.length === 1 && (
                <span className="text-[10px] text-yellow-400/70 ml-1">+1 more needed</span>
              )}
            </span>
          </div>

          {/* Build Optimal button */}
          <div className="flex items-center gap-1.5">
            <select
              value={strategy}
              onChange={e => setStrategy(e.target.value)}
              className="flex-1 bg-ticker-bg border border-ticker-border rounded text-xs text-white px-2 py-1.5"
            >
              {STRATEGIES.map(s => (
                <option key={s.key} value={s.key}>{s.label}</option>
              ))}
            </select>
            <select
              value={numPicks}
              onChange={e => setNumPicks(Number(e.target.value))}
              className="w-14 bg-ticker-bg border border-ticker-border rounded text-xs text-white px-2 py-1.5"
            >
              {[2, 3, 4, 5, 6].map(n => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </div>

          <button
            onClick={handleBuildOptimal}
            disabled={buildingEntry || noPool}
            title={noPool ? 'Build a player pool first via the Lineup tab' : ''}
            className="w-full flex items-center justify-center gap-1.5 px-3 py-2 bg-ticker-green hover:bg-ticker-green/90 text-white text-xs font-semibold rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Sparkles className={`w-3.5 h-3.5 ${buildingEntry ? 'animate-spin' : ''}`} />
            {buildingEntry ? 'Building...' : 'Build Optimal Entry'}
          </button>

          {selectedPicks.length > 0 && (
            <button
              onClick={() => setSelectedPicks([])}
              className="w-full text-xs text-ticker-muted hover:text-ticker-red text-center py-1 transition-colors"
            >
              Clear All
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
