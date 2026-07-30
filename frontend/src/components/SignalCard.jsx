import React, { useState } from 'react'
import {
  AlertTriangle,
  ArrowRightLeft,
  Clock,
  ExternalLink,
  Heart,
  Repeat2,
  Star,
  Users,
  Activity,
  TrendingUp,
} from 'lucide-react'

// ── Signal type config ────────────────────────────────────────────
const SIGNAL_TYPE_CONFIG = {
  rotation_change: { label: 'ROTATION', color: 'text-market-green', bg: 'bg-emerald-900/30', border: 'border-emerald-800/50' },
  injury_update: { label: 'INJURY', color: 'text-market-red', bg: 'bg-red-900/30', border: 'border-red-800/50' },
  minutes_projection: { label: 'MINUTES', color: 'text-cyan-400', bg: 'bg-cyan-900/30', border: 'border-cyan-800/50' },
  lineup_note: { label: 'LINEUP', color: 'text-market-green', bg: 'bg-emerald-900/30', border: 'border-emerald-800/50' },
  trade_rumor: { label: 'TRADE', color: 'text-market-yellow', bg: 'bg-amber-900/30', border: 'border-amber-800/50' },
  general_take: { label: 'TAKE', color: 'text-market-dim', bg: 'bg-gray-800/30', border: 'border-gray-700/50' },
}

// ── Sentiment border colors ───────────────────────────────────────
const SENTIMENT_BORDER = {
  bullish: 'border-l-market-green',
  bearish: 'border-l-market-red',
  neutral: 'border-l-market-yellow',
}

// ── Relative time ─────────────────────────────────────────────────
function timeAgo(dateStr) {
  const date = new Date(dateStr)
  const now = new Date()
  const seconds = Math.floor((now - date) / 1000)
  if (seconds < 60) return 'now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

/**
 * Individual expert signal card with sentiment border, tier badge,
 * signal type pill, content, player mentions, and engagement metrics.
 */
function SignalCard({ signal, onPlayerClick }) {
  const [expanded, setExpanded] = useState(false)

  const sentimentBorder = SENTIMENT_BORDER[signal.sentiment] || 'border-l-market-yellow'
  const typeConfig = SIGNAL_TYPE_CONFIG[signal.signal_type] || SIGNAL_TYPE_CONFIG.general_take

  // Tier badge
  const tierBadge =
    signal.expert_tier === 'tier_1'
      ? { icon: Star, color: 'text-amber-400', fill: true }
      : signal.expert_tier === 'tier_2'
      ? { icon: Star, color: 'text-gray-400', fill: false }
      : null

  const likes = signal.engagement?.likes || 0
  const retweets = signal.engagement?.retweets || 0

  // Truncate content for display
  const MAX_CHARS = 180
  const isLong = signal.content.length > MAX_CHARS
  const displayContent = expanded ? signal.content : signal.content.slice(0, MAX_CHARS)

  return (
    <div
      className={`px-2.5 py-2 border-l-2 ${sentimentBorder} hover:bg-white/[0.02] transition-colors cursor-pointer group`}
      onClick={() => window.open(signal.source_url, '_blank', 'noopener')}
    >
      {/* Row 1: Expert info + signal type */}
      <div className="flex items-center justify-between gap-1 mb-1">
        <div className="flex items-center gap-1.5 min-w-0">
          {/* Avatar placeholder */}
          <div className="w-5 h-5 rounded-full bg-market-border flex items-center justify-center flex-shrink-0">
            <span className="text-[8px] font-bold text-market-dim">
              {signal.expert_name.charAt(0)}
            </span>
          </div>
          {/* Name + handle + tier */}
          <span className="text-[11px] font-bold text-market-text truncate">
            {signal.expert_name}
          </span>
          {tierBadge && (
            <tierBadge.icon
              className={`w-2.5 h-2.5 flex-shrink-0 ${tierBadge.color}`}
              fill={tierBadge.fill ? 'currentColor' : 'none'}
            />
          )}
          <span className="text-[9px] text-market-dim truncate">
            @{signal.expert_handle}
          </span>
        </div>

        {/* Signal type pill */}
        <span
          className={`flex-shrink-0 text-[8px] font-bold uppercase px-1.5 py-0.5 rounded ${typeConfig.bg} ${typeConfig.border} border ${typeConfig.color}`}
        >
          {typeConfig.label}
        </span>
      </div>

      {/* Row 2: Content */}
      <p className="text-[11px] leading-relaxed text-market-text/90 ml-[26px]">
        {displayContent}
        {isLong && !expanded && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              setExpanded(true)
            }}
            className="text-market-yellow hover:text-yellow-300 ml-1"
          >
            more
          </button>
        )}
      </p>

      {/* Row 3: Mentioned players */}
      {signal.mentioned_players && signal.mentioned_players.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-1.5 ml-[26px]">
          {signal.mentioned_players.map((name) => (
            <button
              key={name}
              onClick={(e) => {
                e.stopPropagation()
                onPlayerClick?.(name)
              }}
              className="text-[9px] px-1.5 py-0.5 rounded-full border border-market-green/30 text-market-green/80 hover:border-market-green hover:text-market-green transition-colors"
            >
              {name}
            </button>
          ))}
          {signal.mentioned_team && (
            <span className="text-[9px] px-1.5 py-0.5 rounded-full border border-market-border text-market-dim">
              {signal.mentioned_team}
            </span>
          )}
        </div>
      )}

      {/* Row 4: Meta — time, source, engagement */}
      <div className="flex items-center gap-2 mt-1.5 ml-[26px]">
        <span className="text-[9px] text-market-dim flex items-center gap-0.5">
          <Clock className="w-2.5 h-2.5" />
          {timeAgo(signal.timestamp)}
        </span>
        <span className="text-[9px] text-market-dim">
          {signal.source_platform === 'twitter' ? '𝕏' : '🌐'}
        </span>
        {signal.source_platform === 'twitter' && (likes > 0 || retweets > 0) && (
          <>
            {likes > 0 && (
              <span className="text-[9px] text-market-dim flex items-center gap-0.5">
                <Heart className="w-2.5 h-2.5" />
                {likes >= 1000 ? `${(likes / 1000).toFixed(1)}k` : likes}
              </span>
            )}
            {retweets > 0 && (
              <span className="text-[9px] text-market-dim flex items-center gap-0.5">
                <Repeat2 className="w-2.5 h-2.5" />
                {retweets >= 1000 ? `${(retweets / 1000).toFixed(1)}k` : retweets}
              </span>
            )}
          </>
        )}
        {/* Relevance score badge */}
        <span className="ml-auto text-[8px] text-market-dim opacity-0 group-hover:opacity-100 transition-opacity">
          {signal.relevance_score.toFixed(1)}
        </span>
      </div>
    </div>
  )
}

export default SignalCard
