import React, { useState, useMemo } from 'react'
import {
  Radio,
  RefreshCw,
  ChevronDown,
  ChevronUp,
  AlertTriangle,
  Filter,
} from 'lucide-react'
import useExpertSignals from '../hooks/useExpertSignals'
import SentimentBar from './SentimentBar'
import SignalCard from './SignalCard'

// ── Filter config ─────────────────────────────────────────────────
const TYPE_FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'rotation_change', label: 'Rotation' },
  { key: 'injury_update', label: 'Injury' },
  { key: 'minutes_projection', label: 'Minutes' },
  { key: 'lineup_note', label: 'Lineup' },
  { key: 'trade_rumor', label: 'Trades' },
]

const TIER_FILTERS = [
  { key: 'all', label: 'All Tiers' },
  { key: 'tier_1', label: 'Tier 1' },
  { key: 'tier_1_2', label: 'Tier 1+2' },
]

// ── Skeleton loader ───────────────────────────────────────────────
function SkeletonCard() {
  return (
    <div className="px-2.5 py-2.5 animate-pulse border-l-2 border-l-market-border">
      <div className="flex items-center gap-2 mb-1.5">
        <div className="w-5 h-5 rounded-full bg-market-border" />
        <div className="h-3 bg-market-border rounded w-24" />
        <div className="ml-auto h-3 bg-market-border rounded w-14" />
      </div>
      <div className="ml-[26px] space-y-1.5">
        <div className="h-2.5 bg-market-border rounded w-full" />
        <div className="h-2.5 bg-market-border rounded w-4/5" />
      </div>
      <div className="flex gap-2 mt-2 ml-[26px]">
        <div className="h-2 bg-market-border rounded w-6" />
        <div className="h-2 bg-market-border rounded w-10" />
      </div>
    </div>
  )
}

// ── Filter pill ───────────────────────────────────────────────────
function FilterPill({ label, active, onClick }) {
  return (
    <button
      onClick={onClick}
      className={`px-2 py-0.5 text-[9px] font-bold uppercase rounded-full border transition-colors ${
        active
          ? 'bg-market-yellow/15 border-market-yellow/40 text-market-yellow'
          : 'bg-transparent border-market-border text-market-dim hover:text-market-text hover:border-gray-500'
      }`}
    >
      {label}
    </button>
  )
}

/**
 * Expert Signals panel — left sidebar showing aggregated analyst takes.
 * Features: sentiment bar, type/tier filters, signal cards, auto-refresh.
 */
function ExpertPanel({ teamAbbr, playerNames, onPlayerClick, sport = 'nba' }) {
  const [activeType, setActiveType] = useState('all')
  const [activeTier, setActiveTier] = useState('all')
  const [collapsed, setCollapsed] = useState(false)
  const [showTierFilter, setShowTierFilter] = useState(false)

  const { signals, sentimentSummary, expertsTracked, loading, error, refresh } =
    useExpertSignals(teamAbbr, playerNames, 30, sport)

  // Client-side filtering
  const filteredSignals = useMemo(() => {
    let result = signals

    // Type filter
    if (activeType !== 'all') {
      result = result.filter((s) => s.signal_type === activeType)
    }

    // Tier filter
    if (activeTier === 'tier_1') {
      result = result.filter((s) => s.expert_tier === 'tier_1')
    } else if (activeTier === 'tier_1_2') {
      result = result.filter(
        (s) => s.expert_tier === 'tier_1' || s.expert_tier === 'tier_2'
      )
    }

    return result
  }, [signals, activeType, activeTier])

  // Recompute sentiment for filtered view
  const filteredSentiment = useMemo(() => {
    if (activeType === 'all' && activeTier === 'all') return sentimentSummary
    const total = filteredSignals.length || 1
    return {
      bullish: filteredSignals.filter((s) => s.sentiment === 'bullish').length / total,
      bearish: filteredSignals.filter((s) => s.sentiment === 'bearish').length / total,
      neutral: filteredSignals.filter((s) => s.sentiment === 'neutral').length / total,
    }
  }, [filteredSignals, sentimentSummary, activeType, activeTier])

  return (
    <div
      className="bg-market-surface border border-market-border rounded-lg overflow-hidden flex flex-col"
      style={{ maxHeight: collapsed ? 'auto' : '80vh' }}
    >
      {/* Loading bar */}
      {loading && signals.length > 0 && (
        <div className="h-0.5 bg-market-yellow/30 overflow-hidden">
          <div className="h-full bg-market-yellow animate-pulse w-2/3" />
        </div>
      )}

      {/* Header */}
      <div className="px-3 py-2.5 border-b border-market-border flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-2">
          <Radio className="w-4 h-4 text-market-yellow" />
          <h2 className="text-xs font-bold uppercase tracking-wider text-market-text">
            Expert Signals
          </h2>
          <span className="w-1.5 h-1.5 rounded-full bg-market-yellow animate-pulse" />
        </div>
        <div className="flex items-center gap-1.5">
          <button
            onClick={refresh}
            className="p-1 hover:bg-market-border/30 rounded transition-colors"
            title="Refresh signals"
          >
            <RefreshCw
              className={`w-3 h-3 text-market-dim ${loading ? 'animate-spin' : ''}`}
            />
          </button>
          <button
            onClick={() => setCollapsed((c) => !c)}
            className="p-1 hover:bg-market-border/30 rounded transition-colors"
          >
            {collapsed ? (
              <ChevronDown className="w-3 h-3 text-market-dim" />
            ) : (
              <ChevronUp className="w-3 h-3 text-market-dim" />
            )}
          </button>
        </div>
      </div>

      {/* Subheader: counts */}
      {!collapsed && signals.length > 0 && (
        <div className="px-3 py-1 border-b border-market-border/30">
          <span className="text-[9px] text-market-dim">
            {signals.length} signals &bull; {expertsTracked} experts tracking
          </span>
        </div>
      )}

      {!collapsed && (
        <>
          {/* Sentiment bar */}
          {signals.length > 0 && (
            <SentimentBar sentimentSummary={filteredSentiment} />
          )}

          {/* Type filter pills */}
          <div className="px-3 py-2 border-b border-market-border/30 flex-shrink-0">
            <div className="flex flex-wrap gap-1">
              {TYPE_FILTERS.map((f) => (
                <FilterPill
                  key={f.key}
                  label={f.label}
                  active={activeType === f.key}
                  onClick={() => setActiveType(f.key)}
                />
              ))}
              <button
                onClick={() => setShowTierFilter((s) => !s)}
                className={`p-0.5 rounded transition-colors ${
                  showTierFilter
                    ? 'text-market-yellow'
                    : 'text-market-dim hover:text-market-text'
                }`}
                title="Toggle tier filter"
              >
                <Filter className="w-3 h-3" />
              </button>
            </div>

            {/* Tier filter (toggle) */}
            {showTierFilter && (
              <div className="flex gap-1 mt-1.5">
                {TIER_FILTERS.map((f) => (
                  <FilterPill
                    key={f.key}
                    label={f.label}
                    active={activeTier === f.key}
                    onClick={() => setActiveTier(f.key)}
                  />
                ))}
              </div>
            )}
          </div>

          {/* Signal list */}
          <div className="overflow-y-auto flex-1 divide-y divide-market-border/20">
            {/* Loading skeleton */}
            {loading && signals.length === 0 && (
              <>
                <SkeletonCard />
                <SkeletonCard />
                <SkeletonCard />
                <SkeletonCard />
              </>
            )}

            {/* Error */}
            {error && !loading && (
              <div className="px-3 py-6 text-center">
                <AlertTriangle className="w-5 h-5 text-market-red mx-auto mb-2" />
                <p className="text-[10px] text-market-red">{error}</p>
              </div>
            )}

            {/* Empty */}
            {!loading && !error && filteredSignals.length === 0 && (
              <div className="px-3 py-8 text-center">
                <Radio className="w-8 h-8 text-market-border mx-auto mb-2" />
                <p className="text-[10px] text-market-dim">
                  {signals.length === 0
                    ? 'No expert signals for this slate'
                    : `No ${activeType !== 'all' ? activeType.replace('_', ' ') : ''} signals found`}
                </p>
              </div>
            )}

            {/* Signal cards */}
            {filteredSignals.map((signal) => (
              <SignalCard
                key={signal.id}
                signal={signal}
                onPlayerClick={onPlayerClick}
              />
            ))}
          </div>

          {/* Footer */}
          {signals.length > 0 && (
            <div className="px-3 py-1.5 border-t border-market-border/30 flex-shrink-0">
              <div className="flex items-center justify-between text-[8px] text-market-dim">
                <span>
                  {filteredSignals.length} of {signals.length}
                </span>
                <span className="flex items-center gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-market-yellow animate-pulse" />
                  Auto-refresh 90s
                </span>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

export default ExpertPanel
