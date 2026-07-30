import React from 'react'
import {
  Zap,
  TrendingUp,
  TrendingDown,
  Minus,
  Timer,
  Target,
  ArrowUpDown,
  Gauge,
  Calendar,
  Radio,
  CheckCircle2,
} from 'lucide-react'
import { getLocalToday } from '../utils/dateUtils'

function GameDayBanner({ gameData, selectedTeam, selectedDate }) {
  if (!gameData || !gameData.playing_today) return null

  const todayStr = getLocalToday()
  const isToday = !selectedDate || selectedDate === todayStr

  const game = gameData.game
  const isHome = game.home_team.team_id === selectedTeam?.id

  const teamSide = isHome ? game.home_team : game.away_team
  const oppSide = isHome ? game.away_team : game.home_team
  const teamScore = isHome ? game.projected_home_score : game.projected_away_score
  const oppScore = isHome ? game.projected_away_score : game.projected_home_score

  const spreadLabel =
    game.projected_spread < 0
      ? `${game.home_team.team_abbreviation} -${Math.abs(game.projected_spread)}`
      : game.projected_spread > 0
        ? `${game.away_team.team_abbreviation} -${Math.abs(game.projected_spread)}`
        : 'PICK'

  const paceColor =
    game.pace_label === 'Fast'
      ? 'text-ticker-green'
      : game.pace_label === 'Slow'
        ? 'text-ticker-red'
        : 'text-yellow-400'

  const paceIcon =
    game.pace_label === 'Fast'
      ? TrendingUp
      : game.pace_label === 'Slow'
        ? TrendingDown
        : Minus

  const PaceIcon = paceIcon

  const statusIcon =
    game.game_status === 'In Progress'
      ? Radio
      : game.game_status === 'Final'
        ? CheckCircle2
        : Calendar

  const StatusIcon = statusIcon
  const statusColor =
    game.game_status === 'In Progress'
      ? 'text-ticker-green animate-pulse'
      : game.game_status === 'Final'
        ? 'text-ticker-muted'
        : 'text-blue-400'

  return (
    <div className="mt-4 bg-gradient-to-r from-ticker-card via-ticker-card to-ticker-bg border border-ticker-border rounded-lg overflow-hidden">
      {/* Header strip */}
      <div className="px-4 py-2 border-b border-ticker-border/50 flex items-center justify-between bg-ticker-green/5">
        <div className="flex items-center gap-2">
          <Zap className="w-4 h-4 text-ticker-green" />
          <span className="text-xs font-semibold uppercase tracking-wider text-ticker-green">
            {isToday ? 'Game Day' : 'Scheduled Game'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <StatusIcon className={`w-3.5 h-3.5 ${statusColor}`} />
          <span className="text-xs text-ticker-muted">
            {game.game_status}
            {game.game_time_et ? ` · ${game.game_time_et}` : ''}
          </span>
        </div>
      </div>

      {/* Main content */}
      <div className="px-4 py-4">
        {/* Matchup row */}
        <div className="flex items-center justify-between mb-4">
          {/* Away team */}
          <div className={`flex-1 text-center ${!isHome ? '' : 'opacity-70'}`}>
            <div className="text-2xl font-bold tracking-tight">
              {game.away_team.team_abbreviation}
            </div>
            <div className="text-xs text-ticker-muted mt-0.5">
              {game.away_team.season_ppg} PPG · {game.away_team.last_5_ppg} L5
            </div>
          </div>

          {/* Center: projected score */}
          <div className="flex-shrink-0 px-6 text-center">
            <div className="flex items-center gap-3">
              <span className="text-2xl font-bold text-white tabular-nums">
                {game.projected_away_score}
              </span>
              <div className="flex flex-col items-center">
                <span className="text-[10px] text-ticker-muted uppercase tracking-widest">
                  {isHome ? 'Away' : 'Away'}
                </span>
                <span className="text-lg text-ticker-muted font-light">@</span>
                <span className="text-[10px] text-ticker-muted uppercase tracking-widest">
                  {isHome ? 'Home' : 'Home'}
                </span>
              </div>
              <span className="text-2xl font-bold text-white tabular-nums">
                {game.projected_home_score}
              </span>
            </div>
            <div className="text-[10px] text-ticker-muted mt-1 uppercase tracking-wider">
              Projected Score
            </div>
          </div>

          {/* Home team */}
          <div className={`flex-1 text-center ${isHome ? '' : 'opacity-70'}`}>
            <div className="text-2xl font-bold tracking-tight">
              {game.home_team.team_abbreviation}
            </div>
            <div className="text-xs text-ticker-muted mt-0.5">
              {game.home_team.season_ppg} PPG · {game.home_team.last_5_ppg} L5
            </div>
          </div>
        </div>

        {/* Stats row */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {/* Projected Total */}
          <div className="bg-ticker-bg/50 rounded-lg px-3 py-2.5 border border-ticker-border/30">
            <div className="flex items-center gap-1.5 mb-1">
              <Target className="w-3.5 h-3.5 text-orange-400" />
              <span className="text-[10px] text-ticker-muted uppercase tracking-wider">
                Proj Total
              </span>
            </div>
            <div className="text-lg font-bold text-white tabular-nums">
              {game.projected_total}
            </div>
          </div>

          {/* Over/Under */}
          <div className="bg-ticker-bg/50 rounded-lg px-3 py-2.5 border border-ticker-border/30">
            <div className="flex items-center gap-1.5 mb-1">
              <ArrowUpDown className="w-3.5 h-3.5 text-cyan-400" />
              <span className="text-[10px] text-ticker-muted uppercase tracking-wider">
                O/U Line
              </span>
            </div>
            {game.over_under ? (
              <div className="flex items-baseline gap-1.5">
                <span className="text-lg font-bold text-white tabular-nums">
                  {game.over_under}
                </span>
                <span
                  className={`text-xs font-semibold ${
                    game.over_under_edge > 0 ? 'text-ticker-green' : 'text-ticker-red'
                  }`}
                >
                  {game.over_under_edge > 0 ? '↑ OVER' : '↓ UNDER'}{' '}
                  {Math.abs(game.over_under_edge).toFixed(1)}
                </span>
              </div>
            ) : (
              <div className="text-lg font-bold text-ticker-muted">—</div>
            )}
          </div>

          {/* Spread */}
          <div className="bg-ticker-bg/50 rounded-lg px-3 py-2.5 border border-ticker-border/30">
            <div className="flex items-center gap-1.5 mb-1">
              <TrendingUp className="w-3.5 h-3.5 text-purple-400" />
              <span className="text-[10px] text-ticker-muted uppercase tracking-wider">
                Proj Spread
              </span>
            </div>
            <div className="text-lg font-bold text-white">
              {spreadLabel}
            </div>
          </div>

          {/* Game Pace */}
          <div className="bg-ticker-bg/50 rounded-lg px-3 py-2.5 border border-ticker-border/30">
            <div className="flex items-center gap-1.5 mb-1">
              <Gauge className="w-3.5 h-3.5 text-yellow-400" />
              <span className="text-[10px] text-ticker-muted uppercase tracking-wider">
                Game Pace
              </span>
            </div>
            <div className="flex items-baseline gap-1.5">
              <span className="text-lg font-bold text-white tabular-nums">
                {game.projected_pace}
              </span>
              <div className={`flex items-center gap-0.5 text-xs font-semibold ${paceColor}`}>
                <PaceIcon className="w-3 h-3" />
                {game.pace_label}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export default GameDayBanner
