/**
 * LineupAnalysisPanel — Displays analysis results for a selected lineup.
 *
 * Shows grade, dimension scores, risks, swap suggestions, and portfolio
 * summary (for multi-lineup sets).
 */

import React, { useState, useEffect, useRef } from 'react'
import {
  Award,
  AlertTriangle,
  ArrowRightLeft,
  ChevronDown,
  ChevronUp,
  BarChart3,
  Shield,
  TrendingUp,
  Users,
  Zap,
  Sparkles,
  Loader2,
} from 'lucide-react'
import { rotationAPI } from '../services/api'

const GRADE_COLORS = {
  'A+': 'text-green-400 bg-green-600/20 border-green-500',
  A: 'text-green-400 bg-green-600/15 border-green-500/80',
  'B+': 'text-yellow-400 bg-yellow-600/20 border-yellow-500',
  B: 'text-yellow-400 bg-yellow-600/15 border-yellow-500/80',
  'C+': 'text-orange-400 bg-orange-600/20 border-orange-500',
  C: 'text-orange-400 bg-orange-600/15 border-orange-500/80',
  D: 'text-red-400 bg-red-600/20 border-red-500',
}

const SEVERITY_COLORS = {
  high: 'text-red-400 bg-red-600/10',
  medium: 'text-yellow-400 bg-yellow-600/10',
  low: 'text-green-400 bg-green-600/10',
}

const SEVERITY_DOT = {
  high: 'bg-red-500',
  medium: 'bg-yellow-500',
  low: 'bg-green-500',
}

const DIM_ICONS = {
  projection: Zap,
  floor: Shield,
  ceiling: TrendingUp,
  value: BarChart3,
  stacking: Users,
  expert_consensus: Award,
}

/**
 * Lightweight markdown renderer for AI narrative text.
 * Handles ## headers, **bold**, - bullets, and paragraphs.
 */
function NarrativeRenderer({ text }) {
  if (!text) return null

  const lines = text.split('\n')
  const elements = []
  let bulletBuffer = []
  let key = 0

  const flushBullets = () => {
    if (bulletBuffer.length === 0) return
    elements.push(
      <ul key={key++} className="space-y-1.5 mb-3">
        {bulletBuffer.map((b, i) => (
          <li key={i} className="flex items-start gap-2 text-xs text-gray-300">
            <span className="mt-1.5 w-1 h-1 rounded-full bg-ticker-green/60 flex-shrink-0" />
            <span>{renderInline(b)}</span>
          </li>
        ))}
      </ul>
    )
    bulletBuffer = []
  }

  const renderInline = (str) => {
    // Parse **bold** segments
    const parts = str.split(/(\*\*[^*]+\*\*)/)
    return parts.map((part, i) => {
      if (part.startsWith('**') && part.endsWith('**')) {
        return (
          <span key={i} className="font-semibold text-white">
            {part.slice(2, -2)}
          </span>
        )
      }
      return part
    })
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const trimmed = line.trim()

    // Skip empty lines (flush bullets first)
    if (!trimmed) {
      flushBullets()
      continue
    }

    // ## Section headers
    if (trimmed.startsWith('## ')) {
      flushBullets()
      const heading = trimmed.slice(3)
      // Map headings to icons/colors for visual variety
      let colorClass = 'text-ticker-green'
      if (/risk/i.test(heading)) colorClass = 'text-yellow-400'
      else if (/verdict/i.test(heading)) colorClass = 'text-blue-400'
      else if (/move|swap|recommend/i.test(heading)) colorClass = 'text-purple-400'

      elements.push(
        <h4
          key={key++}
          className={`text-[11px] font-bold uppercase tracking-wider mt-3 mb-1.5 ${colorClass} ${
            key > 1 ? 'border-t border-ticker-border/30 pt-2.5' : ''
          }`}
        >
          {heading}
        </h4>
      )
      continue
    }

    // Bullet points (- item or * item)
    if (/^[-*]\s/.test(trimmed)) {
      bulletBuffer.push(trimmed.replace(/^[-*]\s+/, ''))
      continue
    }

    // Regular paragraph
    flushBullets()
    elements.push(
      <p key={key++} className="text-xs text-gray-300 leading-relaxed mb-2">
        {renderInline(trimmed)}
      </p>
    )
  }

  // Flush any remaining bullets
  flushBullets()

  return <div className="space-y-0">{elements}</div>
}

function CollapsibleSection({ title, icon: Icon, children, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="border-t border-ticker-border/50">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-4 py-2.5 hover:bg-ticker-bg/30 transition-colors"
      >
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-ticker-muted">
          {Icon && <Icon className="w-3.5 h-3.5" />}
          {title}
        </div>
        {open ? (
          <ChevronUp className="w-3.5 h-3.5 text-ticker-muted" />
        ) : (
          <ChevronDown className="w-3.5 h-3.5 text-ticker-muted" />
        )}
      </button>
      {open && <div className="px-4 pb-3">{children}</div>}
    </div>
  )
}

export default function LineupAnalysisPanel({
  analysis,
  selectedIndex = 0,
  platform = 'dk',
  lineups = [],
  draftGroupId,
  gameDate,
  sport = 'nba',
}) {
  const [narrative, setNarrative] = useState('')
  const [narrativeLoading, setNarrativeLoading] = useState(false)
  const [narrativeError, setNarrativeError] = useState(null)
  const narrativeRequestedRef = useRef(false)

  // Auto-fetch narrative when analysis arrives (once per analysis)
  useEffect(() => {
    if (!analysis || lineups.length === 0 || narrativeRequestedRef.current) return
    narrativeRequestedRef.current = true
    setNarrativeLoading(true)
    setNarrativeError(null)
    setNarrative('')

    rotationAPI
      .streamNarrative(
        { platform, draftGroupId, gameDate, lineups, sport },
        (_chunk, fullText) => setNarrative(fullText)
      )
      .then((text) => {
        setNarrative(text)
        setNarrativeLoading(false)
      })
      .catch((err) => {
        setNarrativeError(err.message)
        setNarrativeLoading(false)
      })
  }, [analysis]) // eslint-disable-line react-hooks/exhaustive-deps

  // Reset when analysis changes
  useEffect(() => {
    narrativeRequestedRef.current = false
    setNarrative('')
    setNarrativeError(null)
  }, [lineups.length])

  if (!analysis) return null

  const isDK = platform === 'dk'
  const current = analysis.analyses?.[selectedIndex]
  const portfolio = analysis.portfolio_summary

  if (!current) return null

  const gradeClass =
    GRADE_COLORS[current.overall_grade] || 'text-ticker-muted bg-ticker-bg border-ticker-border'

  return (
    <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden">
      {/* ── Grade Header ──────────────────────────────────── */}
      <div className="px-4 py-3 border-b border-ticker-border flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div
            className={`w-12 h-12 rounded-lg border-2 flex items-center justify-center ${gradeClass}`}
          >
            <span className="text-xl font-black">{current.overall_grade}</span>
          </div>
          <div>
            <h3 className="text-sm font-semibold text-white">
              Lineup #{selectedIndex + 1} Analysis
            </h3>
            <p className="text-xs text-ticker-muted">
              Overall Score: {current.overall_score}/100
            </p>
          </div>
        </div>
        <span className="text-xs text-ticker-muted">
          Efficiency: {current.salary_efficiency}%
        </span>
      </div>

      {/* ── Dimension Scores ──────────────────────────────── */}
      <CollapsibleSection title="Quality Scores" icon={BarChart3}>
        <div className="grid grid-cols-2 gap-2">
          {current.dimension_scores.map((dim) => {
            const Icon = DIM_ICONS[dim.dimension] || Zap
            const pct = (dim.score / 10) * 100
            return (
              <div
                key={dim.dimension}
                className="bg-ticker-bg/50 rounded-md px-3 py-2"
              >
                <div className="flex items-center justify-between mb-1">
                  <div className="flex items-center gap-1.5">
                    <Icon className="w-3 h-3 text-ticker-muted" />
                    <span className="text-[10px] font-semibold uppercase text-ticker-muted">
                      {dim.dimension.replace('_', ' ')}
                    </span>
                  </div>
                  <span
                    className={`text-xs font-bold ${
                      dim.score >= 8
                        ? 'text-green-400'
                        : dim.score >= 6
                        ? 'text-yellow-400'
                        : dim.score >= 4
                        ? 'text-orange-400'
                        : 'text-red-400'
                    }`}
                  >
                    {dim.score.toFixed(1)}
                  </span>
                </div>
                <div className="w-full bg-ticker-border rounded-full h-1.5">
                  <div
                    className={`h-1.5 rounded-full transition-all ${
                      dim.score >= 8
                        ? isDK
                          ? 'bg-green-500'
                          : 'bg-blue-500'
                        : dim.score >= 6
                        ? 'bg-yellow-500'
                        : dim.score >= 4
                        ? 'bg-orange-500'
                        : 'bg-red-500'
                    }`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <p className="text-[10px] text-ticker-muted mt-1 truncate">
                  {dim.detail}
                </p>
              </div>
            )
          })}
        </div>
      </CollapsibleSection>

      {/* ── Risks ─────────────────────────────────────────── */}
      {current.risks.length > 0 && (
        <CollapsibleSection title={`Risks (${current.risks.length})`} icon={AlertTriangle}>
          <div className="space-y-1.5">
            {current.risks.map((risk, i) => (
              <div
                key={i}
                className={`flex items-start gap-2 px-2.5 py-2 rounded ${
                  SEVERITY_COLORS[risk.severity] || 'bg-ticker-bg/50'
                }`}
              >
                <div
                  className={`w-2 h-2 rounded-full mt-1 flex-shrink-0 ${
                    SEVERITY_DOT[risk.severity] || 'bg-ticker-muted'
                  }`}
                />
                <div>
                  <p className="text-xs text-white">{risk.description}</p>
                  {risk.affected_players.length > 0 && (
                    <p className="text-[10px] text-ticker-muted mt-0.5">
                      {risk.affected_players.join(', ')}
                    </p>
                  )}
                </div>
              </div>
            ))}
          </div>
        </CollapsibleSection>
      )}

      {/* ── Swap Suggestions ──────────────────────────────── */}
      {current.swap_suggestions.length > 0 && (
        <CollapsibleSection
          title={`Suggestions (${current.swap_suggestions.length})`}
          icon={ArrowRightLeft}
        >
          <div className="space-y-2">
            {current.swap_suggestions.map((swap, i) => (
              <div
                key={i}
                className="bg-ticker-bg/50 rounded-md px-3 py-2"
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs text-ticker-muted line-through">
                    {swap.current_player}
                  </span>
                  <ArrowRightLeft className="w-3 h-3 text-ticker-muted" />
                  <span className="text-xs font-semibold text-white">
                    {swap.suggested_player}
                  </span>
                  <span
                    className={`text-xs font-bold ml-auto ${
                      swap.projected_fp_delta >= 0 ? 'text-green-400' : 'text-red-400'
                    }`}
                  >
                    {swap.projected_fp_delta >= 0 ? '+' : ''}
                    {swap.projected_fp_delta.toFixed(1)} FP
                  </span>
                </div>
                <p className="text-[10px] text-ticker-muted">{swap.reason}</p>
                <span
                  className={`inline-block mt-1 px-1.5 py-0.5 rounded text-[9px] font-semibold ${
                    swap.confidence === 'high'
                      ? 'bg-green-600/20 text-green-400'
                      : swap.confidence === 'medium'
                      ? 'bg-yellow-600/20 text-yellow-400'
                      : 'bg-ticker-border text-ticker-muted'
                  }`}
                >
                  {swap.confidence} confidence
                </span>
              </div>
            ))}
          </div>
        </CollapsibleSection>
      )}

      {/* ── Correlation Notes ─────────────────────────────── */}
      {current.correlation_notes.length > 0 && (
        <CollapsibleSection title="Game Correlation" icon={Users} defaultOpen={false}>
          <div className="space-y-1">
            {current.correlation_notes.map((note, i) => (
              <p key={i} className="text-xs text-ticker-muted">
                {note}
              </p>
            ))}
          </div>
        </CollapsibleSection>
      )}

      {/* ── Portfolio Summary (multi-lineup) ──────────────── */}
      {portfolio && portfolio.num_lineups > 1 && (
        <CollapsibleSection title="Portfolio Summary" icon={BarChart3} defaultOpen={false}>
          <div className="space-y-3">
            {/* Stats row */}
            <div className="grid grid-cols-3 gap-2 text-center">
              <div className="bg-ticker-bg/50 rounded px-2 py-1.5">
                <div className="text-lg font-bold text-white">
                  {portfolio.unique_players}
                </div>
                <div className="text-[10px] text-ticker-muted">
                  Unique Players
                </div>
              </div>
              <div className="bg-ticker-bg/50 rounded px-2 py-1.5">
                <div
                  className={`text-lg font-bold ${
                    isDK ? 'text-green-400' : 'text-blue-400'
                  }`}
                >
                  {portfolio.avg_projected_fp}
                </div>
                <div className="text-[10px] text-ticker-muted">Avg FP</div>
              </div>
              <div className="bg-ticker-bg/50 rounded px-2 py-1.5">
                <div className="text-lg font-bold text-white">
                  {portfolio.avg_overall_score}
                </div>
                <div className="text-[10px] text-ticker-muted">
                  Avg Score
                </div>
              </div>
            </div>

            {/* Most exposed players */}
            {portfolio.most_exposed_players?.length > 0 && (
              <div>
                <p className="text-[10px] font-semibold text-ticker-muted uppercase mb-1">
                  Most Exposed Players
                </p>
                <div className="space-y-1">
                  {portfolio.most_exposed_players.map((mp, i) => (
                    <div
                      key={i}
                      className="flex items-center justify-between text-xs"
                    >
                      <span className="text-white">{mp.name}</span>
                      <span className="text-ticker-muted font-mono">
                        {mp.count}/{portfolio.num_lineups} ({mp.pct}%)
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Team exposure */}
            {portfolio.team_exposure && (
              <div>
                <p className="text-[10px] font-semibold text-ticker-muted uppercase mb-1">
                  Team Exposure
                </p>
                <div className="space-y-1">
                  {Object.entries(portfolio.team_exposure)
                    .slice(0, 6)
                    .map(([team, pct]) => (
                      <div key={team} className="flex items-center gap-2">
                        <span className="text-xs text-white w-8">{team}</span>
                        <div className="flex-1 bg-ticker-border rounded-full h-1.5">
                          <div
                            className={`h-1.5 rounded-full ${
                              isDK ? 'bg-green-500' : 'bg-blue-500'
                            }`}
                            style={{ width: `${Math.min(pct, 100)}%` }}
                          />
                        </div>
                        <span className="text-[10px] text-ticker-muted font-mono w-10 text-right">
                          {pct}%
                        </span>
                      </div>
                    ))}
                </div>
              </div>
            )}
          </div>
        </CollapsibleSection>
      )}

      {/* ── AI Narrative Insights ─────────────────────────── */}
      <CollapsibleSection title="AI Insights" icon={Sparkles} defaultOpen={true}>
        {narrativeLoading && !narrative && (
          <div className="flex items-center gap-2 py-4 justify-center">
            <Loader2 className="w-4 h-4 animate-spin text-ticker-green" />
            <span className="text-xs text-ticker-muted">Generating AI analysis...</span>
          </div>
        )}
        {narrativeError && (
          <p className="text-xs text-red-400 py-2">
            AI narrative unavailable: {narrativeError}
          </p>
        )}
        {narrative && (
          <div>
            <NarrativeRenderer text={narrative} />
            {narrativeLoading && (
              <span className="inline-block w-1.5 h-3.5 bg-ticker-green/70 ml-0.5 animate-pulse" />
            )}
          </div>
        )}
        {!narrative && !narrativeLoading && !narrativeError && (
          <p className="text-xs text-ticker-muted py-2">
            AI narrative will appear after lineup analysis.
          </p>
        )}
      </CollapsibleSection>
    </div>
  )
}
