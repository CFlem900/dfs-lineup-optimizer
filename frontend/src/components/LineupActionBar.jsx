/**
 * LineupActionBar — Configuration selectors and action buttons for lineup generation.
 *
 * Row 1: Platform toggle, strategy, contest type, mode, lineup count,
 *         exposure slider, recent form weight, lock/exclude info
 * Row 2: Generate, Analyze, Refine, Export options, Clear
 */

import React, { useRef, useState } from 'react'
import {
  RefreshCw,
  Zap,
  Trash2,
  Brain,
  Target,
  Shield,
  TrendingUp,
  Sparkles,
  Rocket,
  Layers,
  Upload,
  Download,
} from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { rotationAPI } from '../services/api'
import LineupExportOptions from './LineupExportOptions'
import StackingPanel from './StackingPanel'
import ImportProjectionsButton from './ImportProjectionsButton'

const LINEUP_COUNTS = [1, 3, 5, 10, 20, 50, 100, 150]

export const STRATEGIES = [
  { key: 'pure_max', label: 'Pure Max', icon: Rocket, tip: 'Maximize raw projected FP — no ceiling blend, minimal noise' },
  { key: 'max_projection', label: 'Max Proj', icon: Target, tip: 'Maximize projected fantasy points (50% proj + 50% upside)' },
  { key: 'balanced', label: 'Balanced', icon: Shield, tip: 'Balance between floor safety and upside' },
  { key: 'ceiling', label: 'Ceiling', icon: TrendingUp, tip: 'Maximize upside potential' },
  { key: 'contrarian', label: 'Contrarian', icon: Sparkles, tip: 'Fade chalk, leverage low-owned high-ceiling plays' },
  { key: 'sim_optimal', label: 'Sim Opt', icon: Brain, tip: 'Monte Carlo ILP — optimal lineups from simulated game worlds' },
  { key: 'sim_filter', label: 'Sim Filter', icon: Layers, tip: 'Simulate 1000 worlds, find most frequent optimal lineups (A/B test)' },
]

export default function LineupActionBar({
  platform, setPlatform, isDK,
  sport = 'nba',
  strategy, setStrategy,
  contestType, setContestType,
  mode, setMode, showdownGameId, setShowdownGameId,
  numLineups, setNumLineups,
  maxExposure, setMaxExposure,
  recentWeight, setRecentWeight,
  optimalityThreshold, setOptimalityThreshold,
  lockedPlayers, excludedPlayers,
  availableGames,
  // New controls
  deterministicMode, setDeterministicMode,
  enableStacking, setEnableStacking,
  // Dynamic stacking overrides (Prompt 5.3)
  primaryStackSize, setPrimaryStackSize,
  secondaryStackSize, setSecondaryStackSize,
  requireBringBack, setRequireBringBack,
  salaryFloorPct, setSalaryFloorPct,
  // Action state
  optimizing, analyzing, refining, poolLoading,
  draftGroupId, lineups, analysis,
  // Handlers
  onGenerate, onAnalyze, onRefine, onClear,
  // Export props
  onExport, onDownloadCSV, onDkUploadOpen, onDkExporterOpen,
  copied, slateMismatch, lineupSlateName, lineupDraftGroupId, currentSlateName,
  isLateSwap = false,
  // Player pool for projection export
  pool = [],
}) {
  return (
    <div className="bg-ticker-card border border-ticker-border rounded-lg px-4 py-3 space-y-3">
      {/* Row 1: Platform + Strategy + Count */}
      <div className="flex items-center justify-between flex-wrap gap-y-2 gap-x-3">
        {/* Platform Toggle */}
        <div className="flex items-center flex-wrap gap-2">
          <div className="flex items-center bg-ticker-bg rounded-md p-0.5">
            <button
              onClick={() => setPlatform('dk')}
              className={`px-3 py-1.5 text-xs font-semibold rounded transition-colors ${
                isDK ? 'bg-green-600 text-white' : 'text-ticker-muted hover:text-white'
              }`}
            >
              DraftKings
            </button>
            <button
              onClick={() => setPlatform('fd')}
              className={`px-3 py-1.5 text-xs font-semibold rounded transition-colors ${
                !isDK ? 'bg-blue-600 text-white' : 'text-ticker-muted hover:text-white'
              }`}
            >
              FanDuel
            </button>
          </div>

          {/* Divider */}
          <div className="w-px h-6 bg-ticker-border" />

          {/* Strategy Selector */}
          <div className="flex items-center bg-ticker-bg rounded-md p-0.5">
            {STRATEGIES.map((s) => {
              const Icon = s.icon
              const isActive = strategy === s.key
              return (
                <button
                  key={s.key}
                  onClick={() => setStrategy(s.key)}
                  title={s.tip}
                  className={`flex items-center gap-1 px-2.5 py-1.5 text-xs font-semibold rounded transition-colors ${
                    isActive
                      ? isDK
                        ? 'bg-green-600/30 text-green-400'
                        : 'bg-blue-600/30 text-blue-400'
                      : 'text-ticker-muted hover:text-white'
                  }`}
                >
                  <Icon className="w-3 h-3" />
                  {s.label}
                </button>
              )
            })}
          </div>

          {/* Contest Type Selector */}
          <div className="flex items-center bg-ticker-bg rounded-md p-0.5">
            {[
              { key: 'gpp', label: 'GPP', tip: 'Tournament — maximize upside + leverage' },
              { key: 'cash', label: 'Cash', tip: 'Cash game — maximize floor safety' },
              { key: 'single_entry', label: 'SE', tip: 'Single entry — balanced approach' },
            ].map((ct) => (
              <button
                key={ct.key}
                onClick={() => setContestType(ct.key)}
                title={ct.tip}
                className={`px-2.5 py-1.5 text-xs font-semibold rounded transition-colors ${
                  contestType === ct.key
                    ? isDK
                      ? 'bg-green-600/30 text-green-400'
                      : 'bg-blue-600/30 text-blue-400'
                    : 'text-ticker-muted hover:text-white'
                }`}
              >
                {ct.label}
              </button>
            ))}
          </div>

          {/* Mode Toggle: Classic / Showdown */}
          {platform === 'dk' && (
            <div className="flex items-center gap-1.5">
              <div className="flex items-center bg-ticker-bg rounded-md p-0.5">
                {[
                  { key: 'classic', label: 'Classic' },
                  { key: 'showdown', label: 'Showdown' },
                ].map((m) => (
                  <button
                    key={m.key}
                    onClick={() => {
                      setMode(m.key)
                      if (m.key === 'classic') setShowdownGameId(null)
                    }}
                    className={`px-2.5 py-1.5 text-xs font-semibold rounded transition-colors ${
                      mode === m.key
                        ? 'bg-yellow-600/30 text-yellow-400'
                        : 'text-ticker-muted hover:text-white'
                    }`}
                  >
                    {m.label}
                  </button>
                ))}
              </div>
              {mode === 'showdown' && availableGames.length > 0 && (
                <select
                  value={showdownGameId || ''}
                  onChange={(e) => setShowdownGameId(e.target.value || null)}
                  className="text-xs bg-ticker-bg border border-ticker-border rounded px-2 py-1.5 text-white"
                >
                  <option value="">Pick game...</option>
                  {availableGames.map((g) => (
                    <option key={g.game_id} value={g.game_id}>
                      {g.label}
                    </option>
                  ))}
                </select>
              )}
            </div>
          )}

          {/* Divider */}
          <div className="w-px h-6 bg-ticker-border" />

          {/* Lineup Count */}
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-ticker-muted">Lineups:</span>
            <div className="flex items-center bg-ticker-bg rounded-md p-0.5">
              {LINEUP_COUNTS.map((n) => (
                <button
                  key={n}
                  onClick={() => setNumLineups(n)}
                  className={`px-2 py-1 text-xs font-semibold rounded transition-colors min-w-[28px] ${
                    numLineups === n
                      ? isDK
                        ? 'bg-green-600/30 text-green-400'
                        : 'bg-blue-600/30 text-blue-400'
                      : 'text-ticker-muted hover:text-white'
                  }`}
                >
                  {n}
                </button>
              ))}
              <input
                type="number"
                min="1"
                max="150"
                value={!LINEUP_COUNTS.includes(numLineups) ? numLineups : ''}
                placeholder="#"
                onChange={(e) => {
                  const v = Math.max(1, Math.min(150, parseInt(e.target.value) || 1))
                  setNumLineups(v)
                }}
                onFocus={() => {
                  if (LINEUP_COUNTS.includes(numLineups)) setNumLineups(numLineups)
                }}
                className={`w-10 px-1 py-1 text-xs font-semibold text-center rounded bg-transparent
                  border border-ticker-border/50 outline-none transition-colors
                  ${
                    !LINEUP_COUNTS.includes(numLineups)
                      ? isDK
                        ? 'border-green-500/50 text-green-400'
                        : 'border-blue-500/50 text-blue-400'
                      : 'text-ticker-muted'
                  }`}
                title="Custom lineup count (1-150)"
              />
            </div>
          </div>
        </div>

        {/* Exposure Limit (shown when generating 3+ lineups) */}
        {numLineups >= 3 && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-ticker-muted whitespace-nowrap">Exposure:</span>
            <input
              type="range"
              min="10"
              max="100"
              step="5"
              value={maxExposure !== null ? Math.round(maxExposure * 100) : 100}
              onChange={(e) => {
                const v = parseInt(e.target.value)
                setMaxExposure(v >= 100 ? null : v / 100)
              }}
              className="w-20 h-1 accent-yellow-500"
            />
            <span className="text-xs text-yellow-400 font-semibold min-w-[36px]">
              {maxExposure !== null ? `${Math.round(maxExposure * 100)}%` : 'Off'}
            </span>
          </div>
        )}

        {/* Game Stacking Toggle */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-ticker-muted whitespace-nowrap">Stacking:</span>
          <button
            onClick={() => setEnableStacking(!enableStacking)}
            className={`px-2.5 py-1 text-xs font-semibold rounded transition-colors ${
              enableStacking
                ? 'bg-purple-600/30 text-purple-400 border border-purple-600/50'
                : 'bg-ticker-bg text-ticker-muted border border-ticker-border'
            }`}
          >
            {enableStacking ? 'ON' : 'OFF'}
          </button>
        </div>

        {/* Recent Form Weight slider */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-ticker-muted whitespace-nowrap">Recent Form:</span>
          <input
            type="range"
            min="0"
            max="60"
            step="5"
            value={recentWeight !== null ? Math.round(recentWeight * 100) : 25}
            onChange={(e) => {
              const v = parseInt(e.target.value)
              setRecentWeight(v === 25 ? null : v / 100)
            }}
            className="w-20 h-1 accent-orange-500"
          />
          <span className="text-xs text-orange-400 font-semibold min-w-[36px]">
            {recentWeight !== null ? `${Math.round(recentWeight * 100)}%` : '25%'}
          </span>
        </div>

        {/* Optimality Floor slider */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-ticker-muted whitespace-nowrap">Opt Floor:</span>
          <input
            type="range"
            min="75"
            max="100"
            step="1"
            value={Math.round(optimalityThreshold * 100)}
            onChange={(e) => setOptimalityThreshold(parseInt(e.target.value) / 100)}
            className="w-20 h-1 accent-cyan-500"
          />
          <span className="text-xs text-cyan-400 font-semibold min-w-[36px]">
            {`${Math.round(optimalityThreshold * 100)}%`}
          </span>
        </div>

        {/* Salary Floor Slider */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-ticker-muted whitespace-nowrap">Salary Min:</span>
          <input
            type="range"
            min="90"
            max="100"
            step="1"
            value={Math.round(salaryFloorPct * 100)}
            onChange={(e) => setSalaryFloorPct(parseInt(e.target.value) / 100)}
            className="w-20 h-1 accent-pink-500"
          />
          <span className="text-xs text-pink-400 font-semibold min-w-[36px]">
            {`${Math.round(salaryFloorPct * 100)}%`}
          </span>
        </div>

        {/* Deterministic Mode */}
        <label className="flex items-center gap-1.5 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={deterministicMode}
            onChange={(e) => setDeterministicMode(e.target.checked)}
            className="w-3.5 h-3.5 rounded border-ticker-border accent-cyan-500"
          />
          <span className="text-xs text-ticker-muted">Reproducible</span>
        </label>

        {/* Locks / Excludes info */}
        {(lockedPlayers.size > 0 || excludedPlayers.size > 0) && (
          <span className="text-xs text-ticker-muted">
            {lockedPlayers.size > 0 && (
              <span className="text-green-400">{lockedPlayers.size} locked</span>
            )}
            {lockedPlayers.size > 0 && excludedPlayers.size > 0 && ' · '}
            {excludedPlayers.size > 0 && (
              <span className="text-red-400">{excludedPlayers.size} excluded</span>
            )}
          </span>
        )}
      </div>

      {/* Row 1.5: Sport-specific stacking controls (Prompt 5.3) ─────
          Renders only when stacking is on AND the sport has a
          ``stackingControls`` schema (NFL / MLB). NBA / CBB still
          rely on the boolean toggle alone — the panel returns null
          for them. */}
      {enableStacking && (
        <StackingPanel
          sport={sport}
          primaryStackSize={primaryStackSize}
          setPrimaryStackSize={setPrimaryStackSize}
          secondaryStackSize={secondaryStackSize}
          setSecondaryStackSize={setSecondaryStackSize}
          requireBringBack={requireBringBack}
          setRequireBringBack={setRequireBringBack}
        />
      )}

      {/* Row 2: Action Buttons */}
      <div className="flex items-center gap-2">
        <button
          onClick={onGenerate}
          disabled={optimizing || poolLoading || !draftGroupId}
          className={`flex items-center gap-1.5 px-4 py-1.5 text-xs font-bold rounded transition-colors
            disabled:opacity-40 ${
              isLateSwap
                ? 'bg-red-600 hover:bg-red-500 text-white'
                : isDK
                  ? 'bg-green-600 hover:bg-green-500 text-white'
                  : 'bg-blue-600 hover:bg-blue-500 text-white'
            }`}
        >
          {optimizing ? (
            <RefreshCw className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <Zap className="w-3.5 h-3.5" />
          )}
          {optimizing
            ? numLineups > 1
              ? `${isLateSwap ? 'Late Swap' : 'Generating'} ${numLineups}...`
              : isLateSwap ? 'Late Swap...' : 'Optimizing...'
            : isLateSwap
            ? numLineups > 1
              ? `Late Swap ${numLineups}`
              : 'Late Swap'
            : numLineups > 1
            ? `Generate ${numLineups}`
            : 'Generate'}
        </button>

        {/* Analyze button - only show when lineups exist */}
        {lineups.length > 0 && (
          <button
            onClick={onAnalyze}
            disabled={analyzing || refining}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded transition-colors
              disabled:opacity-40 border ${
                isDK
                  ? 'border-green-600/50 text-green-400 hover:bg-green-600/10'
                  : 'border-blue-600/50 text-blue-400 hover:bg-blue-600/10'
              }`}
          >
            {analyzing ? (
              <RefreshCw className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Brain className="w-3.5 h-3.5" />
            )}
            {analyzing ? 'Analyzing...' : 'Analyze'}
          </button>
        )}

        {/* Refine button - show when lineups + analysis exist */}
        {lineups.length > 0 && analysis && (
          <button
            onClick={onRefine}
            disabled={refining || analyzing || optimizing}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded transition-colors
              disabled:opacity-40 border ${
                isDK
                  ? 'border-yellow-500/50 text-yellow-400 hover:bg-yellow-600/10'
                  : 'border-yellow-500/50 text-yellow-400 hover:bg-yellow-600/10'
              }`}
            title="Apply swap suggestions to improve lineup grades"
          >
            {refining ? (
              <RefreshCw className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Sparkles className="w-3.5 h-3.5" />
            )}
            {refining ? 'Refining...' : 'Refine'}
          </button>
        )}

        <LineupExportOptions
          lineups={lineups}
          isDK={isDK}
          copied={copied}
          slateMismatch={slateMismatch}
          lineupSlateName={lineupSlateName}
          lineupDraftGroupId={lineupDraftGroupId}
          currentSlateName={currentSlateName}
          draftGroupId={draftGroupId}
          onExport={onExport}
          onDownloadCSV={onDownloadCSV}
          onDkUploadOpen={onDkUploadOpen}
          onDkExporterOpen={onDkExporterOpen}
        />

        <ImportProjectionsButton sport={sport} />
        <ExportProjectionsButton pool={pool} />

        <button
          onClick={onClear}
          disabled={!lineups.length && lockedPlayers.size === 0 && excludedPlayers.size === 0}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold border border-ticker-border
                     rounded hover:bg-ticker-bg transition-colors disabled:opacity-40 text-ticker-muted hover:text-white"
        >
          <Trash2 className="w-3.5 h-3.5" />
          Clear
        </button>
      </div>
    </div>
  )
}


// ImportProjectionsButton was inlined here in Prompt 2.3 and promoted
// to a shared component in Prompt 7.12 so the slate-page Player Pool
// panel can offer the same affordance. Canonical implementation lives
// in ./ImportProjectionsButton.jsx.


/**
 * Export Projections button — downloads a CSV of all player pool projections
 * including minutes, FPPM, FP, ownership, floor/ceiling, and game context.
 */
function ExportProjectionsButton({ pool }) {
  const handleExport = () => {
    if (!pool || pool.length === 0) return

    const headers = [
      'Name', 'Position', 'Team', 'Opp', 'Salary', 'Proj_Minutes',
      'Proj_FP', 'Floor_FP', 'Ceiling_FP', 'FPPM', 'Value',
      'Ownership_Pct', 'Injury_Status', 'Game_Total', 'Spread',
      'Implied_Team_Total', 'Pace', 'Sim_P10', 'Sim_P50', 'Sim_P90',
    ]

    const rows = pool
      .slice()
      .sort((a, b) => (b.projected_fp || 0) - (a.projected_fp || 0))
      .map(p => {
        const mins = p.projected_minutes || 0
        const fp = p.projected_fp || 0
        const fppm = mins > 0 ? (fp / mins).toFixed(3) : '0.000'
        const value = p.salary > 0 ? (fp / (p.salary / 1000)).toFixed(2) : '0.00'
        return [
          p.player_name || p.display_name || '',
          p.position || '',
          p.team_abbreviation || '',
          p.opponent_abbreviation || '',
          p.salary || 0,
          (mins).toFixed(1),
          (fp).toFixed(1),
          (p.floor_fp || 0).toFixed(1),
          (p.ceiling_fp || 0).toFixed(1),
          fppm,
          value,
          p.estimated_ownership != null ? p.estimated_ownership.toFixed(1) : '',
          p.injury_status || '',
          p.game_total != null ? p.game_total.toFixed(1) : '',
          p.vegas_spread != null ? p.vegas_spread.toFixed(1) : '',
          p.implied_team_total != null ? p.implied_team_total.toFixed(1) : '',
          p.game_pace != null ? p.game_pace.toFixed(1) : '',
          p.sim_p10 != null ? p.sim_p10.toFixed(1) : '',
          p.sim_p50 != null ? p.sim_p50.toFixed(1) : '',
          p.sim_p90 != null ? p.sim_p90.toFixed(1) : '',
        ].join(',')
      })

    const csv = [headers.join(','), ...rows].join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `projections_${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <button
      onClick={handleExport}
      disabled={!pool || pool.length === 0}
      className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold border border-ticker-border
                 rounded hover:bg-ticker-bg transition-colors disabled:opacity-40 text-ticker-muted hover:text-white"
      title="Export player projections as CSV"
    >
      <Download className="w-3.5 h-3.5" />
      Export Proj
    </button>
  )
}
