/**
 * PickemCard — A single Underdog pick'em line with edge analysis.
 *
 * Displays player name, UD line, our projection, delta/edge, confidence,
 * and OVER/UNDER recommendation. Clicking toggles selection for entry builder.
 */

import React from 'react'
import { TrendingUp, TrendingDown, Minus, CheckCircle } from 'lucide-react'

const STAT_LABELS = {
  pts: 'PTS',
  reb: 'REB',
  ast: 'AST',
  pra: 'PRA',
  fg3m: '3PM',
  stl_blk: 'STL+BLK',
  fantasy_points: 'FPTS',
  tov: 'TOV',
}

const EDGE_COLORS = {
  strong: 'border-ticker-green',
  moderate: 'border-yellow-500',
  slight: 'border-ticker-border',
  no_edge: 'border-ticker-border',
}

const isUUID = (s) => /^[0-9a-f]{8}-[0-9a-f]{4}/i.test(s || '')

export default function PickemCard({ pick, isSelected, onToggle }) {
  const {
    player_name,
    team_abbreviation,
    stat,
    our_projection,
    ud_line,
    delta,
    delta_pct,
    confidence,
    recommendation,
    edge_strength,
    opponent,
    position,
  } = pick

  // Detect broken data from API parsing issues
  const isBrokenName = !player_name || player_name === '???' || isUUID(player_name)
  const isBrokenTeam = !team_abbreviation || team_abbreviation === '???' || isUUID(team_abbreviation)

  const isOver = recommendation === 'OVER'
  const isUnder = recommendation === 'UNDER'
  const isSkip = recommendation === 'SKIP'

  const recColor = isOver
    ? 'text-ticker-green'
    : isUnder
      ? 'text-ticker-red'
      : 'text-ticker-muted'

  const recBg = isOver
    ? 'bg-ticker-green/10'
    : isUnder
      ? 'bg-red-500/10'
      : 'bg-ticker-card'

  const borderColor = isSelected
    ? 'border-ticker-green ring-1 ring-ticker-green/30'
    : EDGE_COLORS[edge_strength] || 'border-ticker-border'

  // Guard against null/undefined/NaN confidence
  const safeConfidence = typeof confidence === 'number' && !isNaN(confidence) ? confidence : 0
  const confidencePct = Math.round(safeConfidence * 100)

  return (
    <button
      onClick={() => onToggle(pick)}
      className={`relative w-full text-left bg-ticker-card border ${borderColor} rounded-lg p-3 transition-all hover:border-ticker-green/50 hover:bg-ticker-card/80`}
    >
      {/* Selected badge */}
      {isSelected && (
        <div className="absolute -top-1.5 -right-1.5">
          <CheckCircle className="w-5 h-5 text-ticker-green fill-ticker-bg" />
        </div>
      )}

      {/* Header: Player + Team + Position */}
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className={`text-sm font-semibold truncate ${isBrokenName ? 'text-yellow-400 italic' : 'text-white'}`}>
            {isBrokenName ? 'Unknown Player' : player_name}
          </span>
          <span className="text-xs text-ticker-muted flex-shrink-0">
            {isBrokenTeam ? '' : team_abbreviation}
            {position && !isUUID(position) ? ` · ${position}` : ''}
          </span>
        </div>
        {opponent && !isUUID(opponent) && (
          <span className="text-[10px] text-ticker-muted flex-shrink-0">
            vs {opponent}
          </span>
        )}
      </div>

      {/* Stat line + recommendation */}
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-ticker-muted bg-ticker-bg px-1.5 py-0.5 rounded">
            {STAT_LABELS[stat] || stat.toUpperCase()}
          </span>
          <span className="text-lg font-bold text-white">{ud_line}</span>
        </div>
        <div className={`flex items-center gap-1 px-2 py-0.5 rounded text-xs font-bold ${recBg} ${recColor}`}>
          {isOver && <TrendingUp className="w-3 h-3" />}
          {isUnder && <TrendingDown className="w-3 h-3" />}
          {isSkip && <Minus className="w-3 h-3" />}
          {recommendation}
        </div>
      </div>

      {/* Our projection vs UD line */}
      <div className="flex items-center justify-between text-xs mb-2">
        <div className="flex items-center gap-3">
          <span className="text-ticker-muted">
            Ours: <span className="text-white font-medium">{our_projection}</span>
          </span>
          <span className={`font-medium ${delta > 0 ? 'text-ticker-green' : delta < 0 ? 'text-ticker-red' : 'text-ticker-muted'}`}>
            {delta > 0 ? '+' : ''}{delta} ({delta > 0 ? '+' : ''}{delta_pct}%)
          </span>
        </div>
        {/* Edge badge */}
        {edge_strength !== 'no_edge' && (
          <span className={`text-[10px] px-1.5 py-0.5 rounded ${
            edge_strength === 'strong'
              ? 'bg-ticker-green/20 text-ticker-green'
              : edge_strength === 'moderate'
                ? 'bg-yellow-500/20 text-yellow-400'
                : 'bg-ticker-border/50 text-ticker-muted'
          }`}>
            {edge_strength}
          </span>
        )}
      </div>

      {/* Confidence bar */}
      <div className="flex items-center gap-2">
        <div className="flex-1 h-1.5 bg-ticker-bg rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all ${
              confidencePct >= 70
                ? 'bg-ticker-green'
                : confidencePct >= 50
                  ? 'bg-yellow-500'
                  : 'bg-ticker-muted'
            }`}
            style={{ width: `${confidencePct}%` }}
          />
        </div>
        <span className="text-[10px] text-ticker-muted w-8 text-right">
          {confidencePct}%
        </span>
      </div>
    </button>
  )
}
