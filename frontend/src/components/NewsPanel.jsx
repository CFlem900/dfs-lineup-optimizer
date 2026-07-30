import React, { useState, useEffect, useRef, useCallback } from 'react'
import {
  Newspaper,
  RefreshCw,
  AlertTriangle,
  ArrowRightLeft,
  Users,
  Activity,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  Clock,
} from 'lucide-react'
const API_BASE = '/api'

// ── Relevance config ──────────────────────────────────────────────
const RELEVANCE_CONFIG = {
  injury: {
    label: 'INJ',
    color: 'text-red-400',
    bg: 'bg-red-900/30',
    border: 'border-red-800/50',
    icon: AlertTriangle,
  },
  trade: {
    label: 'TRADE',
    color: 'text-yellow-400',
    bg: 'bg-yellow-900/30',
    border: 'border-yellow-800/50',
    icon: ArrowRightLeft,
  },
  lineup: {
    label: 'LINEUP',
    color: 'text-cyan-400',
    bg: 'bg-cyan-900/30',
    border: 'border-cyan-800/50',
    icon: Users,
  },
  general: {
    label: 'NEWS',
    color: 'text-ticker-muted',
    bg: 'bg-gray-800/30',
    border: 'border-gray-700/50',
    icon: Activity,
  },
}

// ── Source icons ───────────────────────────────────────────────────
const SOURCE_STYLES = {
  ESPN: 'text-red-400',
  'NBA.com': 'text-blue-400',
  RotoWire: 'text-green-400',
}

// ── Relative time formatter ───────────────────────────────────────
function timeAgo(dateStr) {
  const date = new Date(dateStr)
  const now = new Date()
  const seconds = Math.floor((now - date) / 1000)

  if (seconds < 60) return 'Just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

// ── Filter pill component ─────────────────────────────────────────
function FilterPill({ label, active, onClick, count }) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1 px-2 py-0.5 text-[10px] font-bold uppercase rounded-full border transition-colors ${
        active
          ? 'bg-ticker-green/20 border-ticker-green/40 text-ticker-green'
          : 'bg-transparent border-ticker-border text-ticker-muted hover:text-white hover:border-gray-500'
      }`}
    >
      {label}
      {count > 0 && (
        <span
          className={`px-1 rounded-full text-[9px] ${
            active ? 'bg-ticker-green/30' : 'bg-ticker-border'
          }`}
        >
          {count}
        </span>
      )}
    </button>
  )
}

// ── Single news item component ────────────────────────────────────
function NewsItemCard({ item, expanded, onToggle }) {
  const relConfig = RELEVANCE_CONFIG[item.relevance] || RELEVANCE_CONFIG.general
  const RelIcon = relConfig.icon
  const sourceColor = SOURCE_STYLES[item.source] || 'text-ticker-muted'

  return (
    <div
      className={`px-3 py-2.5 hover:bg-white/[0.02] transition-colors cursor-pointer border-l-2 ${
        item.relevance === 'injury'
          ? 'border-l-red-500/60'
          : item.relevance === 'trade'
          ? 'border-l-yellow-500/60'
          : item.relevance === 'lineup'
          ? 'border-l-cyan-500/60'
          : 'border-l-transparent'
      }`}
      onClick={onToggle}
    >
      {/* Row 1: Tag + Headline */}
      <div className="flex items-start gap-2">
        <span
          className={`flex-shrink-0 flex items-center gap-1 px-1.5 py-0.5 text-[9px] font-bold uppercase rounded ${relConfig.bg} ${relConfig.border} border ${relConfig.color}`}
        >
          <RelIcon className="w-2.5 h-2.5" />
          {relConfig.label}
        </span>
        <p className="text-xs font-medium leading-snug line-clamp-2 flex-1">
          {item.headline}
        </p>
      </div>

      {/* Row 2: Meta — source, time, teams */}
      <div className="flex items-center gap-2 mt-1.5 ml-[52px]">
        <span className={`text-[10px] font-bold ${sourceColor}`}>
          {item.source}
        </span>
        <span className="text-[10px] text-ticker-muted flex items-center gap-0.5">
          <Clock className="w-2.5 h-2.5" />
          {timeAgo(item.published_at)}
        </span>
        {item.team_names && item.team_names.length > 0 && (
          <span className="text-[10px] text-ticker-muted">
            {item.team_names.join(', ')}
          </span>
        )}
      </div>

      {/* Expanded: Summary + Link */}
      {expanded && item.summary && (
        <div className="mt-2 ml-[52px] space-y-1.5">
          <p className="text-[11px] text-gray-400 leading-relaxed">
            {item.summary}
          </p>
          {item.url && (
            <a
              href={item.url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="inline-flex items-center gap-1 text-[10px] text-ticker-green hover:text-green-300 transition-colors"
            >
              Read more <ExternalLink className="w-2.5 h-2.5" />
            </a>
          )}
        </div>
      )}
    </div>
  )
}

// ── Skeleton loader ───────────────────────────────────────────────
function SkeletonItem() {
  return (
    <div className="px-3 py-2.5 animate-pulse">
      <div className="flex items-start gap-2">
        <div className="w-12 h-4 bg-ticker-border rounded" />
        <div className="flex-1 space-y-1.5">
          <div className="h-3 bg-ticker-border rounded w-full" />
          <div className="h-3 bg-ticker-border rounded w-3/4" />
        </div>
      </div>
      <div className="flex gap-2 mt-2 ml-14">
        <div className="w-8 h-2.5 bg-ticker-border rounded" />
        <div className="w-10 h-2.5 bg-ticker-border rounded" />
      </div>
    </div>
  )
}

// ── Main NewsPanel component ──────────────────────────────────────
function NewsPanel({ teamIds, slateTeamIds, sport = 'nba' }) {
  const [news, setNews] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [activeFilter, setActiveFilter] = useState('all')
  const [expandedId, setExpandedId] = useState(null)
  const [collapsed, setCollapsed] = useState(false)
  const refreshTimerRef = useRef(null)
  const fetchRef = useRef(0)

  // Determine which team IDs to filter by (team selection takes priority)
  const filterTeamIds = teamIds && teamIds.length > 0 ? teamIds : slateTeamIds

  const fetchNews = useCallback(async () => {
    const fetchId = ++fetchRef.current
    setError(null)

    try {
      const params = new URLSearchParams()
      if (filterTeamIds && filterTeamIds.length > 0) {
        filterTeamIds.forEach((id) => params.append('team_id', String(id)))
      }
      params.set('limit', '50')
      if (sport) params.set('sport', sport)
      const res = await fetch(`${API_BASE}/news?${params.toString()}`, { credentials: 'include' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      if (fetchId === fetchRef.current) {
        setNews(data.items || [])
      }
    } catch (err) {
      if (fetchId === fetchRef.current) {
        setError('Failed to load news')
        setNews([])
      }
    } finally {
      if (fetchId === fetchRef.current) {
        setLoading(false)
      }
    }
  }, [filterTeamIds?.join(','), sport]) // eslint-disable-line react-hooks/exhaustive-deps

  // Initial fetch + auto-refresh every 60s (paused when tab hidden or collapsed)
  useEffect(() => {
    setLoading(true)
    fetchNews()

    const tick = () => {
      if (document.hidden || collapsed) return
      fetchNews()
    }
    refreshTimerRef.current = setInterval(tick, 60_000)

    const onVisibility = () => {
      if (!document.hidden && !collapsed) fetchNews()
    }
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      if (refreshTimerRef.current) clearInterval(refreshTimerRef.current)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [fetchNews, collapsed])

  // Client-side filtering
  const filteredNews =
    activeFilter === 'all'
      ? news
      : news.filter((item) => item.relevance === activeFilter)

  // Count by relevance
  const counts = {
    all: news.length,
    injury: news.filter((n) => n.relevance === 'injury').length,
    trade: news.filter((n) => n.relevance === 'trade').length,
    lineup: news.filter((n) => n.relevance === 'lineup').length,
    general: news.filter((n) => n.relevance === 'general').length,
  }

  const handleToggleExpand = (id) => {
    setExpandedId((prev) => (prev === id ? null : id))
  }

  return (
    <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden flex flex-col"
         style={{ maxHeight: collapsed ? 'auto' : '80vh' }}
    >
      {/* Header */}
      <div className="px-3 py-2.5 border-b border-ticker-border flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-2">
          <Newspaper className="w-4 h-4 text-ticker-green" />
          <h2 className="text-xs font-bold uppercase tracking-wider">
            News Feed
          </h2>
          {news.length > 0 && (
            <span className="text-[10px] bg-ticker-green/10 text-ticker-green px-1.5 py-0.5 rounded-full font-bold">
              {news.length}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <button
            onClick={() => {
              setLoading(true)
              fetchNews()
            }}
            className="p-1 hover:bg-ticker-bg rounded transition-colors"
            title="Refresh news"
          >
            <RefreshCw
              className={`w-3 h-3 text-ticker-muted ${loading ? 'animate-spin' : ''}`}
            />
          </button>
          <button
            onClick={() => setCollapsed((c) => !c)}
            className="p-1 hover:bg-ticker-bg rounded transition-colors"
            title={collapsed ? 'Expand' : 'Collapse'}
          >
            {collapsed ? (
              <ChevronDown className="w-3 h-3 text-ticker-muted" />
            ) : (
              <ChevronUp className="w-3 h-3 text-ticker-muted" />
            )}
          </button>
        </div>
      </div>

      {!collapsed && (
        <>
          {/* Filter pills */}
          <div className="px-3 py-2 border-b border-ticker-border/50 flex flex-wrap gap-1.5 flex-shrink-0">
            <FilterPill
              label="All"
              active={activeFilter === 'all'}
              onClick={() => setActiveFilter('all')}
              count={counts.all}
            />
            <FilterPill
              label="Injury"
              active={activeFilter === 'injury'}
              onClick={() => setActiveFilter('injury')}
              count={counts.injury}
            />
            <FilterPill
              label="Trade"
              active={activeFilter === 'trade'}
              onClick={() => setActiveFilter('trade')}
              count={counts.trade}
            />
            <FilterPill
              label="Lineup"
              active={activeFilter === 'lineup'}
              onClick={() => setActiveFilter('lineup')}
              count={counts.lineup}
            />
            <FilterPill
              label="General"
              active={activeFilter === 'general'}
              onClick={() => setActiveFilter('general')}
              count={counts.general}
            />
          </div>

          {/* Content */}
          <div className="overflow-y-auto flex-1 divide-y divide-ticker-border/30">
            {/* Loading skeleton */}
            {loading && news.length === 0 && (
              <>
                <SkeletonItem />
                <SkeletonItem />
                <SkeletonItem />
                <SkeletonItem />
                <SkeletonItem />
              </>
            )}

            {/* Error state */}
            {error && !loading && (
              <div className="px-3 py-6 text-center">
                <AlertTriangle className="w-5 h-5 text-ticker-red mx-auto mb-2" />
                <p className="text-xs text-red-400">{error}</p>
              </div>
            )}

            {/* Empty state */}
            {!loading && !error && filteredNews.length === 0 && (
              <div className="px-3 py-8 text-center">
                <Newspaper className="w-8 h-8 text-ticker-border mx-auto mb-2" />
                <p className="text-xs text-ticker-muted">
                  {news.length === 0
                    ? 'No news available'
                    : `No ${activeFilter} news found`}
                </p>
              </div>
            )}

            {/* News items */}
            {filteredNews.map((item) => (
              <NewsItemCard
                key={item.id}
                item={item}
                expanded={expandedId === item.id}
                onToggle={() => handleToggleExpand(item.id)}
              />
            ))}
          </div>

          {/* Footer */}
          {news.length > 0 && (
            <div className="px-3 py-1.5 border-t border-ticker-border/50 flex-shrink-0">
              <div className="flex items-center justify-between text-[9px] text-ticker-muted">
                <span>
                  {filteredNews.length} of {news.length} items
                </span>
                <span className="flex items-center gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-ticker-green animate-pulse" />
                  Auto-refresh 60s
                </span>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

export default NewsPanel
