import React, { useState, useEffect, useRef } from 'react'
import {
  X,
  RefreshCw,
  AlertCircle,
  Trophy,
  Percent,
  Target,
  TrendingUp,
  TrendingDown,
  ArrowUpDown,
  BarChart3,
  Zap,
  ChevronDown,
  ChevronUp,
} from 'lucide-react'

function GameSimModal({ gameId, simData, loading, error, onClose, onRunSim }) {
  const [platform, setPlatform] = useState('dk')
  const [expandedTeam, setExpandedTeam] = useState('home') // 'home' | 'away' | null

  // Stable ref for onClose so the keydown handler never needs to be re-created
  const onCloseRef = useRef(onClose)
  useEffect(() => { onCloseRef.current = onClose }, [onClose])

  // Close on ESC key — stable handler, registered once
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') onCloseRef.current()
    }
    document.addEventListener('keydown', handleKeyDown)
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      document.body.style.overflow = ''
    }
  }, [])

  const toggleTeam = (team) => {
    setExpandedTeam((prev) => (prev === team ? null : team))
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="sim-modal-title"
      onClick={onClose}
    >
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" />

      {/* Modal */}
      <div
        className="relative bg-ticker-card border border-ticker-border rounded-lg shadow-2xl
                   w-full max-w-3xl max-h-[90vh] flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-4 py-3 border-b border-ticker-border flex items-center justify-between flex-shrink-0">
          <div className="flex items-center gap-3">
            <BarChart3 className="w-4 h-4 text-ticker-green" />
            <h2 id="sim-modal-title" className="text-sm font-bold uppercase tracking-wider">
              Monte Carlo Simulation
            </h2>
            {simData && (
              <span className="text-[10px] text-ticker-muted px-2 py-0.5 bg-ticker-bg rounded">
                {simData.num_simulations.toLocaleString()} sims
              </span>
            )}
          </div>

          <div className="flex items-center gap-3">
            {/* DK/FD Toggle */}
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

            <button
              onClick={onClose}
              aria-label="Close simulation"
              className="p-1 hover:bg-ticker-bg rounded transition-colors"
            >
              <X className="w-4 h-4 text-ticker-muted hover:text-white" />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto">
          {/* Loading */}
          {loading && (
            <div className="py-16 flex flex-col items-center gap-3">
              <RefreshCw className="w-8 h-8 text-ticker-green animate-spin" />
              <p className="text-sm text-ticker-muted">
                Running simulation...
              </p>
              <p className="text-xs text-ticker-muted/60">
                Simulating 10,000 games
              </p>
            </div>
          )}

          {/* Error */}
          {!loading && error && (
            <div className="py-16 flex flex-col items-center gap-3">
              <AlertCircle className="w-8 h-8 text-ticker-red" />
              <p className="text-sm text-red-300">{error}</p>
              <button
                onClick={onRunSim}
                className="mt-2 px-4 py-2 text-xs font-semibold bg-ticker-green rounded hover:bg-ticker-green/80 transition-colors"
              >
                Retry
              </button>
            </div>
          )}

          {/* Results */}
          {!loading && !error && simData && (
            <div className="space-y-0">
              {/* === Game Summary Banner === */}
              <div className="px-4 py-4 border-b border-ticker-border bg-ticker-bg/30">
                {/* Win Probability */}
                <div className="flex items-center justify-between mb-3">
                  <div className="text-center flex-1">
                    <div className="text-lg font-bold">{simData.away_team.team_name}</div>
                    <div className="text-2xl font-bold text-white tabular-nums mt-1">
                      {simData.away_team.mean_score.toFixed(1)}
                    </div>
                    <div className="text-xs text-ticker-muted mt-0.5">
                      ±{simData.away_team.std_score.toFixed(1)}
                    </div>
                  </div>

                  <div className="flex-shrink-0 px-4 text-center">
                    <div className="text-xs text-ticker-muted uppercase tracking-wider mb-1">
                      Win Prob
                    </div>
                    <div className="text-lg text-ticker-muted font-light">vs</div>
                  </div>

                  <div className="text-center flex-1">
                    <div className="text-lg font-bold">{simData.home_team.team_name}</div>
                    <div className="text-2xl font-bold text-white tabular-nums mt-1">
                      {simData.home_team.mean_score.toFixed(1)}
                    </div>
                    <div className="text-xs text-ticker-muted mt-0.5">
                      ±{simData.home_team.std_score.toFixed(1)}
                    </div>
                  </div>
                </div>

                {/* Win Probability Bar */}
                <div className="mb-3">
                  <div className="flex items-center justify-between text-xs font-bold mb-1">
                    <span className="text-orange-400">
                      {(simData.away_win_pct * 100).toFixed(1)}%
                    </span>
                    <span className="text-ticker-muted text-[10px] uppercase tracking-wider">
                      Win Probability
                    </span>
                    <span className="text-ticker-green">
                      {(simData.home_win_pct * 100).toFixed(1)}%
                    </span>
                  </div>
                  <div className="flex h-2.5 rounded-full overflow-hidden bg-ticker-bg border border-ticker-border/50">
                    <div
                      className="bg-orange-400 transition-all duration-500"
                      style={{ width: `${simData.away_win_pct * 100}%` }}
                    />
                    <div
                      className="bg-ticker-green transition-all duration-500"
                      style={{ width: `${simData.home_win_pct * 100}%` }}
                    />
                  </div>
                </div>

                {/* Stat Cards */}
                <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                  {/* Projected Total */}
                  <div className="bg-ticker-card rounded-lg px-3 py-2 border border-ticker-border/30">
                    <div className="flex items-center gap-1.5 mb-0.5">
                      <Target className="w-3 h-3 text-orange-400" />
                      <span className="text-[10px] text-ticker-muted uppercase">Total</span>
                    </div>
                    <div className="text-sm font-bold text-white tabular-nums">
                      {simData.mean_total.toFixed(1)}
                    </div>
                    <div className="text-[10px] text-ticker-muted">
                      ±{simData.std_total.toFixed(1)}
                    </div>
                  </div>

                  {/* Spread */}
                  <div className="bg-ticker-card rounded-lg px-3 py-2 border border-ticker-border/30">
                    <div className="flex items-center gap-1.5 mb-0.5">
                      <TrendingUp className="w-3 h-3 text-purple-400" />
                      <span className="text-[10px] text-ticker-muted uppercase">Spread</span>
                    </div>
                    <div className="text-sm font-bold text-white tabular-nums">
                      {simData.projected_spread > 0
                        ? `${simData.home_team.team_name} -${simData.projected_spread.toFixed(1)}`
                        : simData.projected_spread < 0
                          ? `${simData.away_team.team_name} -${Math.abs(simData.projected_spread).toFixed(1)}`
                          : 'PICK'}
                    </div>
                    <div className="text-[10px] text-ticker-muted">
                      ±{simData.spread_std.toFixed(1)}
                    </div>
                  </div>

                  {/* Over/Under */}
                  <div className="bg-ticker-card rounded-lg px-3 py-2 border border-ticker-border/30">
                    <div className="flex items-center gap-1.5 mb-0.5">
                      <ArrowUpDown className="w-3 h-3 text-cyan-400" />
                      <span className="text-[10px] text-ticker-muted uppercase">O/U</span>
                    </div>
                    {simData.over_under_line != null ? (
                      <>
                        <div className="text-sm font-bold text-white tabular-nums">
                          {simData.over_under_line}
                        </div>
                        <div className="flex gap-2 text-[10px]">
                          <span className={simData.over_pct > 0.5 ? 'text-ticker-green font-semibold' : 'text-ticker-muted'}>
                            O {(simData.over_pct * 100).toFixed(0)}%
                          </span>
                          <span className={simData.under_pct > 0.5 ? 'text-ticker-red font-semibold' : 'text-ticker-muted'}>
                            U {(simData.under_pct * 100).toFixed(0)}%
                          </span>
                        </div>
                      </>
                    ) : (
                      <div className="text-sm font-bold text-ticker-muted">&mdash;</div>
                    )}
                  </div>

                  {/* Score Range */}
                  <div className="bg-ticker-card rounded-lg px-3 py-2 border border-ticker-border/30">
                    <div className="flex items-center gap-1.5 mb-0.5">
                      <BarChart3 className="w-3 h-3 text-yellow-400" />
                      <span className="text-[10px] text-ticker-muted uppercase">Range</span>
                    </div>
                    <div className="text-sm font-bold text-white tabular-nums">
                      {simData.home_team.score_percentiles.p10.toFixed(0)}&ndash;{simData.home_team.score_percentiles.p90.toFixed(0)}
                    </div>
                    <div className="text-[10px] text-ticker-muted">
                      Home p10&ndash;p90
                    </div>
                  </div>
                </div>
              </div>

              {/* === Team Player Tables === */}
              {['away', 'home'].map((side) => {
                const team = side === 'home' ? simData.home_team : simData.away_team
                const isExpanded = expandedTeam === side
                const teamColor = side === 'home' ? 'text-ticker-green' : 'text-orange-400'

                // Sort players by mean DK points descending
                const sorted = [...team.players].sort((a, b) => {
                  const aFP = platform === 'dk' ? a.mean_dk_points : a.mean_fd_points
                  const bFP = platform === 'dk' ? b.mean_dk_points : b.mean_fd_points
                  return bFP - aFP
                })

                return (
                  <div key={side} className="border-b border-ticker-border last:border-b-0">
                    {/* Team Header (collapsible) */}
                    <button
                      onClick={() => toggleTeam(side)}
                      className="w-full px-4 py-3 flex items-center justify-between hover:bg-ticker-bg/30 transition-colors"
                    >
                      <div className="flex items-center gap-3">
                        <Trophy className={`w-4 h-4 ${teamColor}`} />
                        <span className="text-sm font-bold uppercase tracking-wider">
                          {team.team_name}
                        </span>
                        <span className="text-xs text-ticker-muted">
                          {team.mean_score.toFixed(1)} pts · {team.players.length} players
                        </span>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className={`text-xs font-bold ${platform === 'dk' ? 'text-green-400' : 'text-blue-400'}`}>
                          {sorted
                            .reduce((sum, p) => sum + (platform === 'dk' ? p.mean_dk_points : p.mean_fd_points), 0)
                            .toFixed(1)}{' '}
                          FP
                        </span>
                        {isExpanded ? (
                          <ChevronUp className="w-4 h-4 text-ticker-muted" />
                        ) : (
                          <ChevronDown className="w-4 h-4 text-ticker-muted" />
                        )}
                      </div>
                    </button>

                    {/* Player Table */}
                    {isExpanded && (
                      <div className="px-0">
                        <table className="w-full text-sm">
                          <thead>
                            <tr className="text-[10px] text-ticker-muted uppercase tracking-wider border-b border-ticker-border/50">
                              <th className="px-3 py-1.5 text-left">Player</th>
                              <th className="px-2 py-1.5 text-center w-10">Pos</th>
                              <th className="px-2 py-1.5 text-right w-12">Min</th>
                              <th className="px-2 py-1.5 text-right w-12">PTS</th>
                              <th className="px-2 py-1.5 text-right w-12">REB</th>
                              <th className="px-2 py-1.5 text-right w-12">AST</th>
                              <th className="px-2 py-1.5 text-right w-12">STL</th>
                              <th className="px-2 py-1.5 text-right w-12">BLK</th>
                              <th className="px-2 py-1.5 text-right w-14">FP</th>
                              <th className="px-2 py-1.5 text-right w-20">Range</th>
                            </tr>
                          </thead>
                          <tbody>
                            {sorted.map((player) => {
                              const fp =
                                platform === 'dk'
                                  ? player.mean_dk_points
                                  : player.mean_fd_points
                              const pctls =
                                platform === 'dk'
                                  ? player.dk_percentiles
                                  : player.fd_percentiles
                              const stdFP =
                                platform === 'dk'
                                  ? player.std_dk_points
                                  : player.std_fd_points
                              const isInactive = player.mean_minutes < 1

                              return (
                                <tr
                                  key={player.player_id}
                                  className={`border-b border-ticker-border/20 hover:bg-ticker-bg/30 transition-colors ${
                                    isInactive ? 'opacity-30' : ''
                                  }`}
                                >
                                  <td className="px-3 py-1.5">
                                    <span className="whitespace-nowrap text-xs">
                                      {player.player_name}
                                    </span>
                                  </td>
                                  <td className="px-2 py-1.5 text-center">
                                    <span className="px-1.5 py-0.5 bg-ticker-bg rounded text-[10px]">
                                      {player.position}
                                    </span>
                                  </td>
                                  <td className="px-2 py-1.5 text-right text-xs tabular-nums">
                                    {player.mean_minutes.toFixed(1)}
                                  </td>
                                  <td className="px-2 py-1.5 text-right text-xs tabular-nums font-semibold">
                                    {player.mean_pts.toFixed(1)}
                                  </td>
                                  <td className="px-2 py-1.5 text-right text-xs tabular-nums">
                                    {player.mean_reb.toFixed(1)}
                                  </td>
                                  <td className="px-2 py-1.5 text-right text-xs tabular-nums">
                                    {player.mean_ast.toFixed(1)}
                                  </td>
                                  <td className="px-2 py-1.5 text-right text-xs tabular-nums">
                                    {player.mean_stl.toFixed(1)}
                                  </td>
                                  <td className="px-2 py-1.5 text-right text-xs tabular-nums">
                                    {player.mean_blk.toFixed(1)}
                                  </td>
                                  <td className="px-2 py-1.5 text-right font-bold tabular-nums text-xs">
                                    <span
                                      className={
                                        platform === 'dk'
                                          ? 'text-green-400'
                                          : 'text-blue-400'
                                      }
                                    >
                                      {fp.toFixed(1)}
                                    </span>
                                  </td>
                                  <td className="px-2 py-1.5 text-right">
                                    {pctls && !isInactive ? (
                                      <div className="flex items-center justify-end gap-0.5 text-[10px] tabular-nums">
                                        <span className="text-ticker-muted">
                                          {pctls.p10?.toFixed(0)}
                                        </span>
                                        <span className="text-ticker-muted/40">–</span>
                                        <span className="text-white font-semibold">
                                          {pctls.p50?.toFixed(0)}
                                        </span>
                                        <span className="text-ticker-muted/40">–</span>
                                        <span className="text-ticker-muted">
                                          {pctls.p90?.toFixed(0)}
                                        </span>
                                      </div>
                                    ) : (
                                      <span className="text-ticker-muted text-xs">&mdash;</span>
                                    )}
                                  </td>
                                </tr>
                              )
                            })}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          {/* Initial state — not yet run */}
          {!loading && !error && !simData && (
            <div className="py-16 flex flex-col items-center gap-4">
              <BarChart3 className="w-12 h-12 text-ticker-border" />
              <h3 className="text-sm font-semibold text-gray-400">
                Ready to Simulate
              </h3>
              <p className="text-xs text-ticker-muted max-w-sm text-center">
                Run 10,000 Monte Carlo game simulations to generate probability
                distributions for scores, win odds, and player stat lines.
              </p>
              <button
                onClick={onRunSim}
                className="mt-2 flex items-center gap-2 px-5 py-2.5 text-sm font-semibold
                         bg-ticker-green text-white rounded-lg hover:bg-ticker-green/80
                         transition-colors"
              >
                <Zap className="w-4 h-4" />
                Run Simulation
              </button>
            </div>
          )}
        </div>

        {/* Footer */}
        {!loading && simData && (
          <div className="px-4 py-2.5 border-t border-ticker-border flex items-center justify-between text-xs flex-shrink-0">
            <div className="flex items-center gap-2 text-ticker-muted">
              <BarChart3 className="w-3.5 h-3.5" />
              <span>{simData.num_simulations.toLocaleString()} iterations · Monte Carlo</span>
            </div>
            <button
              onClick={onRunSim}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold
                       border border-ticker-border rounded hover:bg-ticker-bg transition-colors"
            >
              <RefreshCw className="w-3 h-3" />
              Re-run
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

export default GameSimModal
