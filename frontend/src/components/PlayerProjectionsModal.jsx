import React, { useState, useEffect, useCallback } from 'react'
import { X, RefreshCw, AlertCircle, Trophy } from 'lucide-react'

function PlayerProjectionsModal({ teamId, rotation, loading, onClose }) {
  const [platform, setPlatform] = useState('dk')

  // Close on ESC key
  const handleKeyDown = useCallback(
    (e) => {
      if (e.key === 'Escape') onClose()
    },
    [onClose]
  )

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown)
    // Prevent body scroll while modal is open
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      document.body.style.overflow = ''
    }
  }, [handleKeyDown])

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

  const teamTotal =
    rotation && platform === 'dk'
      ? rotation.team_dk_total
      : rotation?.team_fd_total

  const sorted = rotation
    ? [...rotation.projections].sort(
        (a, b) => b.adjusted_minutes - a.adjusted_minutes
      )
    : []

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="projections-modal-title"
      onClick={onClose}
    >
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" />

      {/* Modal */}
      <div
        className="relative bg-ticker-card border border-ticker-border rounded-lg shadow-2xl
                   w-full max-w-2xl max-h-[85vh] flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-4 py-3 border-b border-ticker-border flex items-center justify-between flex-shrink-0">
          <div className="flex items-center gap-3">
            <h2 id="projections-modal-title" className="text-sm font-bold uppercase tracking-wider">
              {rotation ? rotation.team_name : 'Loading...'}
            </h2>
            {rotation && (
              <span className="text-xs text-ticker-muted">
                {rotation.game_date}
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

            {/* Close button */}
            <button
              onClick={onClose}
              aria-label="Close projections"
              className="p-1 hover:bg-ticker-bg rounded transition-colors"
            >
              <X className="w-4 h-4 text-ticker-muted hover:text-white" />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="py-12 flex flex-col items-center gap-3">
              <RefreshCw className="w-6 h-6 text-ticker-green animate-spin" />
              <p className="text-sm text-ticker-muted">
                Loading projections...
              </p>
            </div>
          )}

          {!loading && !rotation && (
            <div className="py-12 flex flex-col items-center gap-3">
              <AlertCircle className="w-6 h-6 text-ticker-red" />
              <p className="text-sm text-ticker-muted">
                Failed to load projections.
              </p>
            </div>
          )}

          {!loading && rotation && (
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-ticker-card">
                <tr className="text-xs text-ticker-muted uppercase tracking-wider border-b border-ticker-border">
                  <th className="px-3 py-2 text-left">Player</th>
                  <th className="px-2 py-2 text-center w-10">Pos</th>
                  <th className="px-2 py-2 text-right w-12">Min</th>
                  <th className="px-2 py-2 text-right w-12">FP</th>
                  <th className="px-2 py-2 text-right w-12">Floor</th>
                  <th className="px-2 py-2 text-right w-12">Ceil</th>
                  <th className="px-2 py-2 text-right w-12">PTS</th>
                  <th className="px-2 py-2 text-right w-12">REB</th>
                  <th className="px-2 py-2 text-right w-12">AST</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((player) => {
                  const fp = getFP(player)
                  const floor = getFloor(player)
                  const ceiling = getCeiling(player)
                  const isInjured = player.adjusted_minutes === 0
                  const stats = player.projected_stats || {}

                  return (
                    <tr
                      key={player.player_id}
                      className={`border-b border-ticker-border/30 hover:bg-ticker-bg/30 transition-colors
                                ${isInjured ? 'opacity-40' : ''}`}
                    >
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-1.5">
                          <span
                            className={`whitespace-nowrap ${
                              isInjured ? 'line-through' : ''
                            }`}
                          >
                            {player.player_name}
                          </span>
                          {isInjured && (
                            <AlertCircle className="w-3 h-3 text-ticker-red flex-shrink-0" />
                          )}
                        </div>
                      </td>
                      <td className="px-2 py-2 text-center">
                        <span className="px-1.5 py-0.5 bg-ticker-bg rounded text-xs">
                          {player.position}
                        </span>
                      </td>
                      <td className="px-2 py-2 text-right font-semibold tabular-nums">
                        {player.adjusted_minutes.toFixed(1)}
                      </td>
                      <td className="px-2 py-2 text-right font-bold tabular-nums">
                        {fp != null ? (
                          <span
                            className={
                              platform === 'dk'
                                ? 'text-green-400'
                                : 'text-blue-400'
                            }
                          >
                            {fp.toFixed(1)}
                          </span>
                        ) : (
                          <span className="text-ticker-muted">&mdash;</span>
                        )}
                      </td>
                      <td className="px-2 py-2 text-right text-ticker-muted text-xs tabular-nums">
                        {floor != null ? floor.toFixed(1) : '\u2014'}
                      </td>
                      <td className="px-2 py-2 text-right text-ticker-muted text-xs tabular-nums">
                        {ceiling != null ? ceiling.toFixed(1) : '\u2014'}
                      </td>
                      <td className="px-2 py-2 text-right text-xs tabular-nums">
                        {stats.pts != null ? stats.pts.toFixed(1) : '\u2014'}
                      </td>
                      <td className="px-2 py-2 text-right text-xs tabular-nums">
                        {stats.reb != null ? stats.reb.toFixed(1) : '\u2014'}
                      </td>
                      <td className="px-2 py-2 text-right text-xs tabular-nums">
                        {stats.ast != null ? stats.ast.toFixed(1) : '\u2014'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>

        {/* Footer — Team Total */}
        {!loading && rotation && (
          <div className="px-4 py-2.5 border-t border-ticker-border flex items-center justify-between text-xs flex-shrink-0">
            <div className="flex items-center gap-2 text-ticker-muted">
              <Trophy className="w-3.5 h-3.5" />
              <span>Team Total</span>
            </div>
            <span
              className={`font-bold text-sm ${
                platform === 'dk' ? 'text-green-400' : 'text-blue-400'
              }`}
            >
              {(teamTotal || 0).toFixed(1)} FP
            </span>
          </div>
        )}
      </div>
    </div>
  )
}

export default PlayerProjectionsModal
