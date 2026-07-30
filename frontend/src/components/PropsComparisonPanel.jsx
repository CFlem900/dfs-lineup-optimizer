import React, { useState, useEffect, useCallback, useRef } from 'react'
import { TrendingUp, TrendingDown, Minus, RefreshCw, BarChart3, AlertTriangle } from 'lucide-react'

const API_BASE = '/api'

/**
 * PropsComparisonPanel — Shows DK Sportsbook prop lines vs. our
 * projections for players in the current slate.
 *
 * Props:
 *   playersData  – Array of { teamId, teamAbbr, rotation } (from SlateView)
 *   selectedDate – YYYY-MM-DD string
 */
function PropsComparisonPanel({ playersData, selectedDate, sport = 'nba' }) {
  const [props, setProps] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [sortBy, setSortBy] = useState('delta')  // 'delta' | 'player' | 'stat'
  const fetchRef = useRef(0)

  const fetchProps = useCallback(async () => {
    const fetchId = ++fetchRef.current
    setError(null)
    try {
      const params = { game_date: selectedDate }
      if (sport !== 'nba') params.sport = sport
      const qs = new URLSearchParams(params).toString()
      const res = await fetch(`${API_BASE}/props?${qs}`, { credentials: 'include' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const resData = await res.json()
      if (fetchId !== fetchRef.current) return
      // Flatten props into comparison-style rows
      const rows = []
      const players = resData.players || []
      for (const player of players) {
        for (const prop of (player.props || [])) {
          if (!['pts', 'reb', 'ast', 'pra'].includes(prop.stat)) continue
          rows.push({
            playerName: player.player_name,
            team: player.team_abbreviation,
            stat: prop.stat,
            line: prop.line,
            overOdds: prop.over_odds,
            underOdds: prop.under_odds,
            impliedOverProb: prop.implied_over_prob,
          })
        }
      }
      setProps(rows)
    } catch (err) {
      if (fetchId === fetchRef.current) {
        setError('Unable to load props')
        setProps([])
      }
    } finally {
      if (fetchId === fetchRef.current) {
        setLoading(false)
      }
    }
  }, [selectedDate, sport])

  useEffect(() => {
    setLoading(true)
    fetchProps()
  }, [fetchProps])

  // Merge our projections with prop lines
  const mergedData = React.useMemo(() => {
    if (!playersData || !props.length) return []

    // Build lookup: "lowercase_name:TEAM" → player projection
    const projLookup = {}
    for (const team of (playersData || [])) {
      if (!team.rotation?.projections) continue
      for (const p of team.rotation.projections) {
        const key = `${p.player_name.toLowerCase()}:${team.teamAbbr}`
        projLookup[key] = {
          pts: p.projected_stats?.pts || 0,
          reb: p.projected_stats?.reb || 0,
          ast: p.projected_stats?.ast || 0,
          pra: (p.projected_stats?.pts || 0) + (p.projected_stats?.reb || 0) + (p.projected_stats?.ast || 0),
        }
      }
    }

    return props.map((prop) => {
      const key = `${prop.playerName.toLowerCase()}:${prop.team}`
      const proj = projLookup[key]
      const ourVal = proj ? (proj[prop.stat] || 0) : 0
      const delta = ourVal - prop.line
      const deltaPct = prop.line > 0 ? (delta / prop.line) * 100 : 0
      let signal = 'aligned'
      if (Math.abs(deltaPct) > 15) signal = deltaPct > 0 ? 'bullish' : 'bearish'
      else if (Math.abs(deltaPct) > 5) signal = deltaPct > 0 ? 'bullish' : 'bearish'

      return {
        ...prop,
        ourVal: Math.round(ourVal * 10) / 10,
        delta: Math.round(delta * 10) / 10,
        deltaPct: Math.round(deltaPct * 10) / 10,
        signal,
      }
    }).filter((r) => r.ourVal > 0)
  }, [playersData, props])

  // Sort
  const sorted = React.useMemo(() => {
    const data = [...mergedData]
    if (sortBy === 'delta') {
      data.sort((a, b) => Math.abs(b.deltaPct) - Math.abs(a.deltaPct))
    } else if (sortBy === 'player') {
      data.sort((a, b) => a.playerName.localeCompare(b.playerName))
    } else if (sortBy === 'stat') {
      data.sort((a, b) => a.stat.localeCompare(b.stat) || a.playerName.localeCompare(b.playerName))
    }
    return data
  }, [mergedData, sortBy])

  // Summary stats
  const summary = React.useMemo(() => {
    if (!sorted.length) return null
    const bullish = sorted.filter((r) => r.deltaPct > 5).length
    const bearish = sorted.filter((r) => r.deltaPct < -5).length
    const aligned = sorted.length - bullish - bearish
    const avgAbsDelta = sorted.reduce((s, r) => s + Math.abs(r.deltaPct), 0) / sorted.length
    return { bullish, bearish, aligned, avgAbsDelta: Math.round(avgAbsDelta * 10) / 10 }
  }, [sorted])

  const signalColor = (signal, deltaPct) => {
    if (Math.abs(deltaPct) <= 5) return 'text-ticker-muted'
    if (Math.abs(deltaPct) <= 15) return deltaPct > 0 ? 'text-yellow-400' : 'text-yellow-400'
    return deltaPct > 0 ? 'text-ticker-green' : 'text-ticker-red'
  }

  const signalBg = (deltaPct) => {
    if (Math.abs(deltaPct) <= 5) return ''
    if (Math.abs(deltaPct) <= 15) return 'bg-yellow-400/5'
    return deltaPct > 0 ? 'bg-ticker-green/5' : 'bg-ticker-red/5'
  }

  const SignalIcon = ({ deltaPct }) => {
    if (Math.abs(deltaPct) <= 5) return <Minus className="w-3 h-3 text-ticker-muted" />
    if (deltaPct > 0) return <TrendingUp className="w-3 h-3 text-ticker-green" />
    return <TrendingDown className="w-3 h-3 text-ticker-red" />
  }

  const formatOdds = (odds) => {
    if (!odds) return '—'
    return odds > 0 ? `+${odds}` : `${odds}`
  }

  const statLabel = (stat) => {
    const labels = { pts: 'PTS', reb: 'REB', ast: 'AST', pra: 'PRA', fg3m: '3PM' }
    return labels[stat] || stat.toUpperCase()
  }

  return (
    <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden flex flex-col">
      {/* Header */}
      <div className="px-3 py-2 border-b border-ticker-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <BarChart3 className="w-4 h-4 text-ticker-green" />
          <span className="text-sm font-semibold text-white">
            Props vs. Projections
          </span>
          {sorted.length > 0 && (
            <span className="px-1.5 py-0.5 bg-ticker-green/10 text-ticker-green text-[10px] font-bold rounded-full">
              {sorted.length}
            </span>
          )}
        </div>
        <button
          onClick={() => { setLoading(true); fetchProps() }}
          className="p-1 rounded hover:bg-ticker-bg/50 transition-colors"
          title="Refresh props"
        >
          <RefreshCw className={`w-3.5 h-3.5 text-ticker-muted ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {/* Summary bar */}
      {summary && (
        <div className="px-3 py-1.5 bg-ticker-bg/30 border-b border-ticker-border/50 flex items-center gap-3 text-[10px]">
          <span className="text-ticker-green font-semibold">
            ▲ {summary.bullish} bullish
          </span>
          <span className="text-ticker-red font-semibold">
            ▼ {summary.bearish} bearish
          </span>
          <span className="text-ticker-muted">
            — {summary.aligned} aligned
          </span>
          <span className="ml-auto text-ticker-muted">
            avg |Δ| {summary.avgAbsDelta}%
          </span>
        </div>
      )}

      {/* Sort controls */}
      <div className="px-3 py-1 border-b border-ticker-border/50 flex gap-1">
        {['delta', 'player', 'stat'].map((key) => (
          <button
            key={key}
            onClick={() => setSortBy(key)}
            className={`px-2 py-0.5 text-[10px] font-medium rounded transition-colors ${
              sortBy === key
                ? 'bg-ticker-green/15 text-ticker-green'
                : 'text-ticker-muted hover:text-white'
            }`}
          >
            {key === 'delta' ? 'By Delta' : key === 'player' ? 'By Player' : 'By Stat'}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto max-h-[50vh]">
        {loading && sorted.length === 0 && (
          <div className="px-3 py-8 text-center">
            <div className="animate-pulse flex flex-col items-center gap-2">
              <div className="h-4 w-24 bg-ticker-border/50 rounded" />
              <div className="h-3 w-32 bg-ticker-border/30 rounded" />
            </div>
          </div>
        )}

        {error && !loading && (
          <div className="px-3 py-6 text-center">
            <AlertTriangle className="w-5 h-5 text-yellow-500 mx-auto mb-1" />
            <p className="text-xs text-ticker-muted">{error}</p>
          </div>
        )}

        {!loading && !error && sorted.length === 0 && (
          <div className="px-3 py-8 text-center">
            <BarChart3 className="w-5 h-5 text-ticker-muted mx-auto mb-1" />
            <p className="text-xs text-ticker-muted">No props available</p>
            <p className="text-[10px] text-ticker-muted/60 mt-0.5">
              Props appear when DK Sportsbook publishes player lines
            </p>
          </div>
        )}

        {sorted.length > 0 && (
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-ticker-card z-[1]">
              <tr className="text-[10px] text-ticker-muted uppercase tracking-wider border-b border-ticker-border">
                <th className="px-2 py-1.5 text-left">Player</th>
                <th className="px-1 py-1.5 text-center w-10">Stat</th>
                <th className="px-1 py-1.5 text-center w-12">Ours</th>
                <th className="px-1 py-1.5 text-center w-12">Market</th>
                <th className="px-1 py-1.5 text-center w-14">Δ</th>
                <th className="px-1 py-1.5 text-center w-10">O/U</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((row, i) => (
                <tr
                  key={`${row.playerName}-${row.stat}-${i}`}
                  className={`border-b border-ticker-border/20 hover:bg-ticker-bg/30 transition-colors ${signalBg(row.deltaPct)}`}
                >
                  <td className="px-2 py-1.5">
                    <div className="flex items-center gap-1.5">
                      <SignalIcon deltaPct={row.deltaPct} />
                      <span className="text-white font-medium truncate max-w-[100px]">
                        {row.playerName}
                      </span>
                      <span className="text-ticker-muted text-[10px]">{row.team}</span>
                    </div>
                  </td>
                  <td className="px-1 py-1.5 text-center">
                    <span className="px-1 py-0.5 bg-ticker-border/30 rounded text-[10px] font-medium text-ticker-muted">
                      {statLabel(row.stat)}
                    </span>
                  </td>
                  <td className="px-1 py-1.5 text-center font-medium text-white tabular-nums">
                    {row.ourVal}
                  </td>
                  <td className="px-1 py-1.5 text-center text-ticker-muted tabular-nums">
                    {row.line}
                  </td>
                  <td className={`px-1 py-1.5 text-center font-semibold tabular-nums ${signalColor(row.signal, row.deltaPct)}`}>
                    {row.delta > 0 ? '+' : ''}{row.delta}
                    <span className="text-[10px] ml-0.5 opacity-60">
                      ({row.deltaPct > 0 ? '+' : ''}{row.deltaPct}%)
                    </span>
                  </td>
                  <td className="px-1 py-1.5 text-center text-[10px] text-ticker-muted tabular-nums">
                    {formatOdds(row.overOdds)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Footer */}
      {sorted.length > 0 && (
        <div className="px-3 py-1.5 border-t border-ticker-border/50 text-[10px] text-ticker-muted flex items-center justify-between">
          <span>DK Sportsbook lines</span>
          <span className="flex items-center gap-1">
            <span className="inline-block w-2 h-2 rounded-full bg-ticker-green/40" /> aligned
            <span className="inline-block w-2 h-2 rounded-full bg-yellow-400/40 ml-1" /> mild
            <span className="inline-block w-2 h-2 rounded-full bg-ticker-red/40 ml-1" /> divergent
          </span>
        </div>
      )}
    </div>
  )
}

export default PropsComparisonPanel
