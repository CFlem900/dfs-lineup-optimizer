import React from 'react'
import {
  TrendingUp,
  TrendingDown,
  Minus,
  Gauge,
  Target,
  ArrowUpDown,
  Radio,
  CheckCircle2,
  Calendar,
  BarChart3,
} from 'lucide-react'

function GameSlateCard({ game, sport = 'nba', onOpenTeamModal, onSimulate }) {
  // Sports we don't yet have a stats engine for: hide PPG / pace / projected
  // scores entirely rather than showing 0.0 placeholders. The matchup, time,
  // and status are real (from ESPN); the rest stays a "Coming soon" stub
  // until we wire in a per-sport projection engine.
  const showBasketballStats = sport === 'nba' || sport === 'cbb'

  // Weather chip (MLB only) — Prompt 4.4. The backend's
  // mlb_weather_service attaches a {temp, wind_speed, wind_direction,
  // precip_prob, condition} dict for outdoor games and a {condition:
  // "Dome", precip_prob: 0} sentinel for closed-roof parks. Unknown /
  // fetch-failed games come through with weather=null and we render
  // nothing, keeping the card clean.
  const showWeather = sport === 'mlb' && game.weather != null
  const isDome = showWeather && game.weather.condition === 'Dome'
  const isOutdoor = showWeather && game.weather.condition === 'Outdoor'

  // Postponement-risk badge (Prompt 7.3) — surfaces high precipitation
  // probability so users don't lock in players for a game that's about
  // to be rained out (PPD = $0 fantasy points). Two tiers:
  //   1–39%   → grey "☔ X% Rain" (informational)
  //   40–100% → red  "⚠️ X% Rain Risk" (PPD warning, hard to miss)
  // Domes ship precip_prob=0 so the badge never renders for them,
  // satisfying the acceptance criterion.
  const precipProb = showWeather ? (game.weather.precip_prob ?? 0) : 0
  const showRainSoft = precipProb > 0 && precipProb < 40
  const showRainHard = precipProb >= 40

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

  const PaceIcon =
    game.pace_label === 'Fast'
      ? TrendingUp
      : game.pace_label === 'Slow'
        ? TrendingDown
        : Minus

  const StatusIcon =
    game.game_status === 'In Progress'
      ? Radio
      : game.game_status === 'Final'
        ? CheckCircle2
        : Calendar

  const statusColor =
    game.game_status === 'In Progress'
      ? 'text-ticker-green animate-pulse'
      : game.game_status === 'Final'
        ? 'text-ticker-muted'
        : 'text-blue-400'

  return (
    <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden hover:border-ticker-muted/50 transition-colors">
      {/* Status strip */}
      <div className="px-3 py-2 border-b border-ticker-border/50 flex items-center justify-between bg-ticker-bg/30">
        <div className="flex items-center gap-2">
          <StatusIcon className={`w-3.5 h-3.5 ${statusColor}`} />
          <span className="text-xs text-ticker-muted">
            {game.game_status}
            {game.game_time_et ? ` · ${game.game_time_et}` : ''}
          </span>
        </div>
        {showBasketballStats && (
          <div className={`flex items-center gap-1 text-xs font-semibold ${paceColor}`}>
            <PaceIcon className="w-3 h-3" />
            {game.pace_label}
          </div>
        )}
        {/* MLB weather chip — Prompt 4.4. Dome → static badge.
            Outdoor → live temp + wind (mph). The wind direction
            isn't surfaced visually here; the backend wind multiplier
            already factored it into adjusted_fp on the player side. */}
        {isDome && (
          <span
            className="flex items-center gap-1 text-[11px] font-semibold text-cyan-400 bg-cyan-600/10 border border-cyan-600/30 rounded px-1.5 py-0.5"
            title="Closed-roof venue — weather has no effect"
          >
            🏟️ Dome
          </span>
        )}
        {isOutdoor && (
          <span
            className="flex items-center gap-1 text-[11px] font-semibold text-orange-300 bg-orange-600/10 border border-orange-600/30 rounded px-1.5 py-0.5 tabular-nums"
            title={`Wind ${Math.round(game.weather.wind_direction)}° · Open-Meteo forecast`}
          >
            {Math.round(game.weather.temp)}°F
            <span className="text-ticker-muted">|</span>
            💨 {Math.round(game.weather.wind_speed)}mph
          </span>
        )}
        {/* Rain badges (Prompt 7.3) — soft (1-39%) and hard (≥40%).
            The hard variant uses a louder color/icon to make
            postponement risk hard to miss; the soft variant keeps
            the card readable when the chance is non-trivial but
            unlikely to PPD the game. */}
        {showRainSoft && (
          <span
            className="flex items-center gap-1 text-[11px] font-semibold text-gray-300 bg-gray-600/15 border border-gray-500/30 rounded px-1.5 py-0.5 tabular-nums"
            title={`Open-Meteo forecast: ${precipProb}% chance of precipitation at first pitch`}
          >
            ☔ {precipProb}% Rain
          </span>
        )}
        {showRainHard && (
          <span
            className="flex items-center gap-1 text-[11px] font-bold text-red-200 bg-red-600/25 border border-red-500/60 rounded px-1.5 py-0.5 tabular-nums animate-pulse"
            title={
              `HIGH POSTPONEMENT RISK\n` +
              `Open-Meteo forecast: ${precipProb}% chance of precipitation at first pitch.\n` +
              `Consider waiting for an official lineup confirmation before locking players in this game.`
            }
          >
            ⚠️ {precipProb}% Rain Risk
          </span>
        )}
      </div>

      {/* Matchup — team names are clickable */}
      <div className="px-4 py-4">
        <div className="flex items-center justify-between mb-1">
          {/* Away */}
          <div className="flex-1 text-center">
            <button
              onClick={() => onOpenTeamModal(game.away_team.team_id)}
              className="text-xl font-bold tracking-tight hover:text-ticker-green
                         transition-colors cursor-pointer underline decoration-ticker-border
                         hover:decoration-ticker-green underline-offset-4"
            >
              {game.away_team.team_abbreviation}
            </button>
            {showBasketballStats && (
              <>
                <div className="text-lg font-bold text-white tabular-nums mt-0.5">
                  {game.projected_away_score}
                </div>
                <div className="text-[10px] text-ticker-muted mt-0.5">
                  {game.away_team.season_ppg} PPG
                </div>
              </>
            )}
          </div>

          {/* Divider */}
          <div className="flex-shrink-0 px-3 text-center">
            <span className="text-sm text-ticker-muted font-light">@</span>
          </div>

          {/* Home */}
          <div className="flex-1 text-center">
            <button
              onClick={() => onOpenTeamModal(game.home_team.team_id)}
              className="text-xl font-bold tracking-tight hover:text-ticker-green
                         transition-colors cursor-pointer underline decoration-ticker-border
                         hover:decoration-ticker-green underline-offset-4"
            >
              {game.home_team.team_abbreviation}
            </button>
            {showBasketballStats && (
              <>
                <div className="text-lg font-bold text-white tabular-nums mt-0.5">
                  {game.projected_home_score}
                </div>
                <div className="text-[10px] text-ticker-muted mt-0.5">
                  {game.home_team.season_ppg} PPG
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Stats row \u2014 basketball-only stats hidden for sports without
          a projection engine yet. NFL/MLB show a friendly placeholder. */}
      {showBasketballStats ? (
        <div className="px-3 py-2 border-t border-ticker-border/50 grid grid-cols-2 gap-2">
          <div className="flex items-center gap-1.5">
            <Target className="w-3 h-3 text-orange-400" />
            <span className="text-[10px] text-ticker-muted uppercase">Total</span>
            <span className="text-xs font-bold text-white tabular-nums ml-auto">
              {game.projected_total}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <TrendingUp className="w-3 h-3 text-purple-400" />
            <span className="text-[10px] text-ticker-muted uppercase">Spread</span>
            <span className="text-xs font-bold text-white ml-auto">{spreadLabel}</span>
          </div>
          <div className="flex items-center gap-1.5">
            <Gauge className="w-3 h-3 text-yellow-400" />
            <span className="text-[10px] text-ticker-muted uppercase">Pace</span>
            <span className="text-xs font-bold text-white tabular-nums ml-auto">
              {game.projected_pace}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <ArrowUpDown className="w-3 h-3 text-cyan-400" />
            <span className="text-[10px] text-ticker-muted uppercase">O/U</span>
            <span className="text-xs font-bold text-ticker-muted ml-auto">
              {game.over_under || '\u2014'}
            </span>
          </div>
        </div>
      ) : (
        <div className="px-3 py-2 border-t border-ticker-border/50 text-center">
          <span className="text-[10px] text-ticker-muted uppercase tracking-wider">
            Projections coming soon \u00b7 use Import Proj to upload a CSV
          </span>
        </div>
      )}

      {/* Simulate button — basketball-only (Prompt 7.11). The Monte
          Carlo sim engine models minutes-distribution + per-player
          scoring rates, which is structurally an NBA / CBB
          abstraction. MLB needs a hitter/pitcher matchup model;
          NFL needs play-by-play distributions. Surfacing a button
          that always returns "rotation data unavailable" is worse
          than no button — hide it entirely until per-sport sim
          engines ship. */}
      {showBasketballStats && (
        <div className="px-3 py-2 border-t border-ticker-border/50">
          <button
            onClick={() => onSimulate(game.game_id, game.over_under)}
            className="w-full flex items-center justify-center gap-2 py-1.5 text-xs font-semibold
                     text-ticker-green border border-ticker-green/30 rounded
                     hover:bg-ticker-green/10 transition-colors"
          >
            <BarChart3 className="w-3.5 h-3.5" />
            Simulate
          </button>
        </div>
      )}
    </div>
  )
}

export default GameSlateCard
