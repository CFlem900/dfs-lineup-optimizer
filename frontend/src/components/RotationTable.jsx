import React, { useState } from 'react'
import { TrendingUp, TrendingDown, Minus, AlertCircle } from 'lucide-react'

function RotationTable({ rotation }) {
  const [platform, setPlatform] = useState('dk') // 'dk' or 'fd'

  if (!rotation || !rotation.projections) return null

  const sorted = [...rotation.projections].sort(
    (a, b) => b.adjusted_minutes - a.adjusted_minutes
  )

  const maxMinutes = Math.max(...sorted.map((p) => p.adjusted_minutes))

  // Check if DFS data is available
  const hasDFS = sorted.some((p) => p.dk_points != null)

  const getDelta = (p) => {
    return +(p.adjusted_minutes - p.baseline_minutes).toFixed(1)
  }

  const getDeltaColor = (delta) => {
    if (delta > 1) return 'text-ticker-green'
    if (delta < -1) return 'text-ticker-red'
    return 'text-ticker-muted'
  }

  const getDeltaIcon = (delta) => {
    if (delta > 1) return <TrendingUp className="w-3.5 h-3.5" />
    if (delta < -1) return <TrendingDown className="w-3.5 h-3.5" />
    return <Minus className="w-3.5 h-3.5" />
  }

  const getBarColor = (minutes) => {
    if (minutes >= 32) return 'bg-ticker-green'
    if (minutes >= 20) return 'bg-blue-500'
    if (minutes >= 10) return 'bg-yellow-500'
    return 'bg-ticker-muted'
  }

  const getFP = (player) => {
    if (player.adjusted_minutes === 0) return null
    return platform === 'dk' ? player.dk_points : player.fd_points
  }

  const getFloor = (player) => {
    if (player.adjusted_minutes === 0) return null
    return platform === 'dk' ? player.dk_floor : player.fd_floor
  }

  const getCeiling = (player) => {
    if (player.adjusted_minutes === 0) return null
    return platform === 'dk' ? player.dk_ceiling : player.fd_ceiling
  }

  return (
    <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-ticker-border flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wider">
          Projected Minutes {hasDFS && '& Fantasy'}
        </h2>
        <div className="flex items-center gap-3">
          {hasDFS && (
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
          )}
          <span className="text-xs text-ticker-muted">
            {rotation.team_name} | {rotation.game_date}
          </span>
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm whitespace-nowrap">
          <thead>
            <tr className="text-xs text-ticker-muted uppercase tracking-wider border-b border-ticker-border">
              <th className="px-2 py-2 text-left w-8">#</th>
              <th className="px-2 py-2 text-left min-w-[120px]">Player</th>
              <th className="px-2 py-2 text-center w-10">Pos</th>
              <th className="px-2 py-2 text-right w-14">Base</th>
              <th className="px-2 py-2 text-right w-14">Proj</th>
              <th className="px-2 py-2 text-right w-16">+/-</th>
              {hasDFS && (
                <>
                  <th className="px-2 py-2 text-right w-14">FP</th>
                  <th className="px-2 py-2 text-right w-14">Floor</th>
                  <th className="px-2 py-2 text-right w-14">Ceil</th>
                </>
              )}
              <th className="px-2 py-2 text-left w-28">Dist</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((player, idx) => {
              const delta = getDelta(player)
              const barWidth =
                maxMinutes > 0
                  ? (player.adjusted_minutes / maxMinutes) * 100
                  : 0
              const isInjured = player.adjusted_minutes === 0
              const fp = getFP(player)
              const floor = getFloor(player)
              const ceiling = getCeiling(player)

              return (
                <tr
                  key={player.player_id}
                  className={`border-b border-ticker-border/50 hover:bg-ticker-bg/30 transition-colors
                            ${isInjured ? 'opacity-40' : ''}`}
                >
                  <td className="px-2 py-2 text-ticker-muted">{idx + 1}</td>
                  <td className="px-2 py-2">
                    <div className="flex items-center gap-1.5">
                      <span className={isInjured ? 'line-through' : ''}>
                        {player.player_name}
                      </span>
                      {isInjured && (
                        <AlertCircle className="w-3.5 h-3.5 text-ticker-red" />
                      )}
                    </div>
                  </td>
                  <td className="px-2 py-2 text-center">
                    <span className="px-1.5 py-0.5 bg-ticker-bg rounded text-xs">
                      {player.position}
                    </span>
                  </td>
                  <td className="px-2 py-2 text-right text-ticker-muted">
                    {player.baseline_minutes.toFixed(1)}
                  </td>
                  <td className="px-2 py-2 text-right font-semibold">
                    {player.adjusted_minutes.toFixed(1)}
                  </td>
                  <td
                    className={`px-2 py-2 text-right ${getDeltaColor(delta)}`}
                  >
                    <div className="flex items-center justify-end gap-1">
                      {getDeltaIcon(delta)}
                      <span>
                        {delta > 0 ? '+' : ''}
                        {delta.toFixed(1)}
                      </span>
                    </div>
                  </td>
                  {hasDFS && (
                    <>
                      <td className="px-2 py-2 text-right font-semibold">
                        {fp != null ? (
                          <span className={platform === 'dk' ? 'text-green-400' : 'text-blue-400'}>
                            {fp.toFixed(1)}
                          </span>
                        ) : (
                          <span className="text-ticker-muted">&mdash;</span>
                        )}
                      </td>
                      <td className="px-2 py-2 text-right text-ticker-muted text-xs">
                        {floor != null ? floor.toFixed(1) : '\u2014'}
                      </td>
                      <td className="px-2 py-2 text-right text-ticker-muted text-xs">
                        {ceiling != null ? ceiling.toFixed(1) : '\u2014'}
                      </td>
                    </>
                  )}
                  <td className="px-2 py-2">
                    <div className="w-full bg-ticker-bg rounded-full h-2">
                      <div
                        className={`h-2 rounded-full bar-fill ${getBarColor(
                          player.adjusted_minutes
                        )}`}
                        style={{ '--bar-width': `${barWidth}%`, width: `${barWidth}%` }}
                      />
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* Footer */}
      <div className="px-4 py-2.5 border-t border-ticker-border flex items-center justify-between text-xs text-ticker-muted">
        <span>
          Total: <span className="text-white font-semibold">{rotation.total_minutes.toFixed(1)}</span> min
        </span>
        <div className="flex items-center gap-4">
          {hasDFS && (
            <span>
              Team FP:{' '}
              <span className={`font-semibold ${platform === 'dk' ? 'text-green-400' : 'text-blue-400'}`}>
                {platform === 'dk'
                  ? (rotation.team_dk_total || 0).toFixed(1)
                  : (rotation.team_fd_total || 0).toFixed(1)}
              </span>
            </span>
          )}
          <span>
            Players: <span className="text-white">{rotation.projections.length}</span>
          </span>
        </div>
      </div>
    </div>
  )
}

export default RotationTable
