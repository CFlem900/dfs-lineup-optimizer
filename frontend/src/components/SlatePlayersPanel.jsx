import React, { useMemo, useState } from 'react'
import { Users, RefreshCw, AlertCircle, AlertTriangle, Trophy, Download } from 'lucide-react'
import ImportProjectionsButton from './ImportProjectionsButton'


// ─────────────────────────────────────────────────────────────────────
// Per-sport per-stat column config (Prompt 7.8)
// ─────────────────────────────────────────────────────────────────────
//
// Replaces hardcoded PTS/REB/AST headers with sport-aware columns. Each
// entry maps ``sport`` → an array of ``{key, label}`` objects where
// ``key`` is the field name on ``player.projected_stats`` and ``label``
// is what the column header shows.
//
//   NBA / CBB : PTS / REB / AST  (highest-leverage scoring stats)
//   MLB       : HR / RBI / R     (hitter-focused — DK MLB scoring is
//                                 dominated by HR (+10), RBI (+2),
//                                 R (+2). Pitchers show em-dashes here
//                                 because their stat profile lives in
//                                 a disjoint key space (K / IP / W);
//                                 the FP / Floor / Ceil columns still
//                                 carry meaningful pitcher info.)
//   NFL       : empty array       (NFL stats are position-specific —
//                                 QB cares about pass_yd/pass_td,
//                                 RB about rush_yd, WR/TE about rec.
//                                 Showing one set would leave most
//                                 rows mostly empty. Skip until a
//                                 follow-up adds position-aware
//                                 column rendering.)
//
// Adding a sport is a single-entry edit here — no JSX changes needed.
const SPORT_STAT_COLUMNS = {
  nba: [
    { key: 'pts', label: 'PTS' },
    { key: 'reb', label: 'REB' },
    { key: 'ast', label: 'AST' },
  ],
  cbb: [
    { key: 'pts', label: 'PTS' },
    { key: 'reb', label: 'REB' },
    { key: 'ast', label: 'AST' },
  ],
  mlb: [
    { key: 'hr',  label: 'HR'  },
    { key: 'rbi', label: 'RBI' },
    { key: 'r',   label: 'R'   },
  ],
  nfl: [],
}


// Single-letter rotation-role chip (Prompt 7.8). Rendered inline with
// the player name for basketball sports so users can see at a glance
// whether the optimizer's "viable" pool is mostly starters or
// includes bench / out players.
function RoleBadge({ role }) {
  if (!role) return null
  const config = {
    Starter: {
      letter: 'S',
      classes: 'bg-green-900/40 text-green-300 border-green-700/40',
      tooltip: 'Starter (projected ≥ 28 min)',
    },
    Bench: {
      letter: 'B',
      classes: 'bg-yellow-900/40 text-yellow-300 border-yellow-700/40',
      tooltip: 'Bench rotation (projected < 28 min)',
    },
    Out: {
      letter: 'X',
      classes: 'bg-red-900/40 text-red-300 border-red-700/40',
      tooltip: 'Out / inactive',
    },
  }
  const cfg = config[role]
  if (!cfg) return null
  return (
    <span
      title={cfg.tooltip}
      className={`inline-flex items-center justify-center w-4 h-4 rounded text-[9px] font-bold leading-none border flex-shrink-0 ${cfg.classes}`}
    >
      {cfg.letter}
    </span>
  )
}


function SlatePlayersPanel({ playersData, loading, error, onRetry, sport = 'nba' }) {
  const [platform, setPlatform] = useState('dk')

  // Polymorphic rendering — basketball sports get the minutes column;
  // others (NFL has snap counts, MLB has no minutes) hide it. The
  // per-stat columns are driven separately by ``SPORT_STAT_COLUMNS``
  // above so each sport surfaces its own metrics rather than just
  // hiding NBA's.
  const isBasketball = sport === 'nba' || sport === 'cbb'
  const statColumns = SPORT_STAT_COLUMNS[sport] || []

  // Flatten all team rotations into a single player list (deduplicated).
  // Wrapped in useMemo so re-renders triggered by platform / sport
  // toggles don't redo the O(N) flatten + dedupe pass on every render.
  // Client-side grouping happens upstream (App.jsx reshapes the flat
  // /player-pool response into team-grouped form); this loop just
  // unfolds it back into a flat list for sorting + display.
  const allPlayers = useMemo(() => {
    if (!playersData) return []
    const out = []
    const seenIds = new Set()
    for (const team of playersData) {
      if (!team.rotation || !team.rotation.projections) continue
      for (const p of team.rotation.projections) {
        if (seenIds.has(p.player_id)) continue
        seenIds.add(p.player_id)
        out.push({
          ...p,
          teamAbbr: team.teamAbbr,
        })
      }
    }
    return out
  }, [playersData])

  // Separate active and injured, sort active by FP descending
  const activePlayers = allPlayers.filter((p) => p.adjusted_minutes > 0)
  const injuredPlayers = allPlayers.filter((p) => p.adjusted_minutes === 0)

  const getFP = (player) =>
    platform === 'dk' ? player.dk_points : player.fd_points
  const getFloor = (player) =>
    platform === 'dk' ? player.dk_floor : player.fd_floor
  const getCeiling = (player) =>
    platform === 'dk' ? player.dk_ceiling : player.fd_ceiling

  const sortedActive = [...activePlayers].sort(
    (a, b) => (getFP(b) || 0) - (getFP(a) || 0)
  )
  const sortedInjured = [...injuredPlayers].sort((a, b) =>
    a.player_name.localeCompare(b.player_name)
  )
  const sorted = [...sortedActive, ...sortedInjured]

  const totalFP = activePlayers.reduce(
    (sum, p) => sum + (getFP(p) || 0),
    0
  )

  return (
    <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden flex flex-col">
      {/* Header */}
      <div className="px-4 py-3 border-b border-ticker-border flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-semibold uppercase tracking-wider">
            Player Pool
          </h2>
          {!loading && allPlayers.length > 0 && (
            <span className="px-2 py-0.5 bg-ticker-green/10 text-ticker-green text-xs font-bold rounded-full">
              {activePlayers.length}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {/* Import Proj button (Prompt 7.12) — same shared component
              the Lineup tab uses, so users can upload a projections
              CSV directly from the slate page where the empty-state
              copy ("USE IMPORT PROJ TO UPLOAD A CSV") points them.
              ``compact`` shrinks the padding to fit the header. */}
          <ImportProjectionsButton sport={sport} compact />
          <button
            onClick={() => {
              if (!sorted.length) return
              const plat = platform
              // CSV columns vary by sport — basketball includes
              // Minutes + FPPM (FP per minute), other sports replace
              // those with just FP since minutes aren't tracked.
              const headers = isBasketball
                ? ['Name','Pos','Team','Salary','Minutes','FP','Floor','Ceiling','FPPM','Value','Injury']
                : ['Name','Pos','Team','Salary','FP','Floor','Ceiling','Value','Injury']
              const rows = sorted.map(p => {
                const fp = plat === 'dk' ? (p.dk_points || 0) : (p.fd_points || 0)
                const floor = plat === 'dk' ? (p.dk_floor || 0) : (p.fd_floor || 0)
                const ceil = plat === 'dk' ? (p.dk_ceiling || 0) : (p.fd_ceiling || 0)
                const mins = p.adjusted_minutes || 0
                const fppm = mins > 0 ? (fp / mins).toFixed(3) : '0.000'
                const sal = p.dk_salary || p.fd_salary || 0
                const value = sal > 0 ? (fp / (sal / 1000)).toFixed(2) : '0.00'
                const injury = p.play_probability < 1 ? p.reason || 'Q/GTD' : ''
                const cols = isBasketball
                  ? [
                      `"${p.player_name}"`, p.position, p.teamAbbr || '',
                      sal, mins.toFixed(1), fp.toFixed(1), floor.toFixed(1), ceil.toFixed(1),
                      fppm, value, `"${injury}"`,
                    ]
                  : [
                      `"${p.player_name}"`, p.position, p.teamAbbr || '',
                      sal, fp.toFixed(1), floor.toFixed(1), ceil.toFixed(1),
                      value, `"${injury}"`,
                    ]
                return cols.join(',')
              })
              const csv = [headers.join(','), ...rows].join('\n')
              const blob = new Blob([csv], { type: 'text/csv' })
              const url = URL.createObjectURL(blob)
              const a = document.createElement('a')
              a.href = url
              a.download = `slate_projections_${new Date().toISOString().slice(0,10)}.csv`
              a.click()
              URL.revokeObjectURL(url)
            }}
            disabled={!sorted.length}
            className="p-1.5 text-ticker-muted hover:text-ticker-green transition-colors disabled:opacity-30"
            title="Export projections as CSV"
          >
            <Download className="w-3.5 h-3.5" />
          </button>
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
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto max-h-[70vh]">
        {loading && (
          <div className="py-12 flex flex-col items-center gap-3">
            <RefreshCw className="w-6 h-6 text-ticker-green animate-spin" />
            <p className="text-sm text-ticker-muted">
              Loading projections...
            </p>
          </div>
        )}

        {!loading && sorted.length === 0 && (
          <div className="py-12 flex flex-col items-center gap-3 px-4 text-center">
            {error ? (
              <>
                <AlertTriangle className="w-8 h-8 text-yellow-500" />
                <p className="text-sm text-yellow-400">{error}</p>
                {onRetry && (
                  <button
                    onClick={onRetry}
                    className="mt-2 flex items-center gap-2 px-3 py-1.5 text-xs font-semibold
                             bg-ticker-green/10 text-ticker-green border border-ticker-green/30
                             rounded hover:bg-ticker-green/20 transition-colors"
                  >
                    <RefreshCw className="w-3.5 h-3.5" />
                    Retry
                  </button>
                )}
              </>
            ) : (
              <>
                <Users className="w-8 h-8 text-ticker-border" />
                <p className="text-sm text-ticker-muted">
                  No player data available
                </p>
              </>
            )}
          </div>
        )}

        {!loading && sorted.length > 0 && (
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-ticker-card z-[1]">
              <tr className="text-[10px] text-ticker-muted uppercase tracking-wider border-b border-ticker-border">
                <th className="px-3 py-2 text-left">Player</th>
                <th className="px-1 py-2 text-center w-10">Team</th>
                <th className="px-1 py-2 text-center w-9">Pos</th>
                <th className="px-1.5 py-2 text-right w-14">Sal</th>
                {/* Min: NBA / CBB only — NFL uses snap counts, MLB has
                    no minutes concept */}
                {isBasketball && (
                  <th className="px-1.5 py-2 text-right w-11">Min</th>
                )}
                <th className="px-1.5 py-2 text-right w-11">FP</th>
                <th className="px-1.5 py-2 text-right w-11">Val</th>
                <th className="px-1.5 py-2 text-right w-11">Flr</th>
                <th className="px-1.5 py-2 text-right w-11">Ceil</th>
                {/* Sport-aware per-stat columns. NBA/CBB get PTS/REB/AST,
                    MLB gets HR/RBI/R, NFL gets none. Driven by
                    ``SPORT_STAT_COLUMNS`` so adding a sport is a
                    single-entry edit. */}
                {statColumns.map((col) => (
                  <th
                    key={col.key}
                    className="px-1.5 py-2 text-right w-10"
                  >
                    {col.label}
                  </th>
                ))}
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
                    key={`${player.teamAbbr}-${player.player_id}`}
                    className={`border-b border-ticker-border/20 hover:bg-ticker-bg/30 transition-colors ${
                      isInjured ? 'opacity-40' : ''
                    }`}
                  >
                    <td className="px-3 py-1.5">
                      <div className="flex items-center gap-1.5">
                        <span
                          className={`whitespace-nowrap text-xs ${
                            isInjured ? 'line-through' : ''
                          }`}
                        >
                          {player.player_name}
                        </span>
                        {/* Rotation-role chip (Prompt 7.8) — only
                            meaningful for basketball, where the
                            backend tiers by minutes. Skipped for
                            NFL / MLB because their starter_min_minutes=0
                            collapses every active player to "Starter"
                            (the chip would always read "S" — no info). */}
                        {isBasketball && player.rotation_role && (
                          <RoleBadge role={player.rotation_role} />
                        )}
                        {isInjured && (
                          <AlertCircle className="w-3 h-3 text-ticker-red flex-shrink-0" />
                        )}
                      </div>
                    </td>
                    <td className="px-1 py-1.5 text-center">
                      <span className="px-1.5 py-0.5 bg-ticker-bg rounded text-[10px] font-semibold text-ticker-muted">
                        {player.teamAbbr}
                      </span>
                    </td>
                    <td className="px-1 py-1.5 text-center">
                      <span className="px-1.5 py-0.5 bg-ticker-bg rounded text-[10px]">
                        {player.position}
                      </span>
                    </td>
                    <td className="px-1.5 py-1.5 text-right text-xs tabular-nums text-ticker-muted">
                      {player.dk_salary != null && !isInjured
                        ? `$${(player.dk_salary / 1000).toFixed(1)}K`
                        : '\u2014'}
                    </td>
                    {/* Min cell: render only for basketball sports. */}
                    {isBasketball && (
                      <td className="px-1.5 py-1.5 text-right text-xs tabular-nums">
                        {player.adjusted_minutes.toFixed(1)}
                      </td>
                    )}
                    <td className="px-1.5 py-1.5 text-right text-xs font-bold tabular-nums">
                      {fp != null && !isInjured ? (
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
                    <td className="px-1.5 py-1.5 text-right text-xs font-bold tabular-nums">
                      {player.dk_value != null && !isInjured ? (
                        <span
                          className={
                            player.dk_value >= 5.0
                              ? 'text-yellow-300'
                              : player.dk_value >= 4.0
                                ? 'text-green-400'
                                : 'text-ticker-muted'
                          }
                        >
                          {player.dk_value.toFixed(1)}x
                        </span>
                      ) : (
                        <span className="text-ticker-muted">&mdash;</span>
                      )}
                    </td>
                    <td className="px-1.5 py-1.5 text-right text-[10px] text-ticker-muted tabular-nums">
                      {floor != null && !isInjured
                        ? floor.toFixed(1)
                        : '\u2014'}
                    </td>
                    <td className="px-1.5 py-1.5 text-right text-[10px] text-ticker-muted tabular-nums">
                      {ceiling != null && !isInjured
                        ? ceiling.toFixed(1)
                        : '\u2014'}
                    </td>
                    {/* Per-stat cells, driven by the same sport-aware
                        config that owns the headers. ``projected_stats``
                        is a generic dict so the lookup works uniformly
                        across sports \u2014 NBA pulls pts/reb/ast keys,
                        MLB pulls hr/rbi/r, etc. */}
                    {statColumns.map((col) => {
                      const v = stats[col.key]
                      return (
                        <td
                          key={col.key}
                          className="px-1.5 py-1.5 text-right text-xs tabular-nums"
                        >
                          {v != null ? Number(v).toFixed(1) : '\u2014'}
                        </td>
                      )
                    })}
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Footer */}
      {!loading && sorted.length > 0 && (
        <div className="px-4 py-2.5 border-t border-ticker-border flex items-center justify-between text-xs flex-shrink-0">
          <div className="flex items-center gap-2 text-ticker-muted">
            <Users className="w-3.5 h-3.5" />
            <span>{activePlayers.length} active · {injuredPlayers.length} out</span>
          </div>
          <div className="flex items-center gap-2">
            <Trophy className="w-3.5 h-3.5 text-ticker-muted" />
            <span
              className={`font-bold ${
                platform === 'dk' ? 'text-green-400' : 'text-blue-400'
              }`}
            >
              {totalFP.toFixed(1)} FP
            </span>
          </div>
        </div>
      )}
    </div>
  )
}

export default SlatePlayersPanel
