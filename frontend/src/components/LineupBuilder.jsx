/**
 * LineupBuilder — Main orchestrating component for the DFS Lineup tab.
 *
 * Layout:
 * 1. Slate tabs (Early / Main / Night) — inherited from existing slate data
 * 2. Platform toggle (DK / FD) + Strategy + Lineup count + Generate / Analyze / Export / Clear
 * 3. LineupGrid (multi) or LineupDisplay (single) + LineupPlayerPool
 * 4. LineupAnalysisPanel (when analysis is available)
 *
 * All state logic lives in useLineupState hook.
 * UI is composed from LineupActionBar, LineupGenerationState, and LineupExportOptions.
 */

import React, { forwardRef } from 'react'
import { Calendar, Sun, Tv, Moon, Zap, Radio } from 'lucide-react'
import LineupDisplay from './LineupDisplay'
import LineupPlayerPool from './LineupPlayerPool'
import LineupGrid from './LineupGrid'
import LineupAnalysisPanel from './LineupAnalysisPanel'
import DKUploadModal from './DKUploadModal'
import DKExporterModal from './DKExporterModal'
import LineupActionBar, { STRATEGIES } from './LineupActionBar'
import LineupGenerationState from './LineupGenerationState'
import { getDkRosterSlots } from '../config/sportShapes'
import useLineupState from '../hooks/useLineupState'

const SLATE_ICONS = { Early: Sun, Main: Tv, Night: Moon, 'All Games': Tv }
const SLATE_ACTIVE_COLORS = {
  Early: 'bg-yellow-500 text-black',
  Main: 'bg-ticker-green text-white',
  Night: 'bg-purple-500 text-white',
  'All Games': 'bg-ticker-green text-white',
}

const LineupBuilder = forwardRef(function LineupBuilder({ slateData, selectedDate, onLineupsGenerated, sport = 'nba' }, ref) {
  const state = useLineupState({ slateData, selectedDate, onLineupsGenerated, sport, ref })

  const {
    platform, setPlatform, setActiveSlate,
    pool, displayPool, excludedPool, poolLoading, poolProgress, hasOverrides, projectionOverrides,
    lineups, baselineScore, baselineLineup, selectedLineupIdx, setSelectedLineupIdx,
    numLineups, setNumLineups,
    strategy, setStrategy,
    contestType, setContestType,
    mode, setMode, showdownGameId, setShowdownGameId,
    maxExposure, setMaxExposure,
    recentWeight, setRecentWeight,
    optimalityThreshold, setOptimalityThreshold,
    deterministicMode, setDeterministicMode,
    enableStacking, setEnableStacking,
    primaryStackSize, setPrimaryStackSize,
    secondaryStackSize, setSecondaryStackSize,
    requireBringBack, setRequireBringBack,
    salaryFloorPct, setSalaryFloorPct,
    optimizing, optimizeError, setOptimizeError,
    lineupDraftGroupId,
    analysis, analyzing,
    refining, refineSteps, lastRefineResult, setLastRefineResult,
    lockedPlayers, excludedPlayers,
    copied, dkUploadOpen, setDkUploadOpen, dkExporterOpen, setDkExporterOpen,
    gridSortConfig, setGridSortConfig,
    optimizeSteps, analyzeSteps,
    hasSlates, slates, currentSlateName, draftGroupId, isDK,
    isLateSwap,
    slateMismatch, lineupSlateName, availableGames, selectedLineup, lineupPlayerIds,
    isToday,
    handleGenerate, handleAnalyze, handleRefine,
    handleExport, handleDownloadCSV,
    handleClear,
    toggleLock, toggleExclude,
    handleProjectionEdit, handleResetPlayer, handleResetAll,
    handleRefreshPool,
  } = state

  // ── No-data state ──────────────────────────────────────────────
  // When the slate has no games (offseason, off day, or a sport that
  // hasn't released a slate yet), we still surface the per-sport roster
  // shape so the user can confirm at a glance what they're switching
  // into — NBA = 8 slots, NFL = 9, MLB = 10, etc.
  if (!slateData || slateData.game_count === 0) {
    const previewSlots = getDkRosterSlots(sport)
    return (
      <div className="mt-16 text-center">
        <Calendar className="w-16 h-16 text-ticker-border mx-auto mb-4" />
        <h2 className="text-lg font-semibold text-gray-400 mb-2">
          {isToday ? 'No Games Today' : 'No Games on This Date'}
        </h2>
        <p className="text-sm text-ticker-muted max-w-md mx-auto mb-6">
          No DraftKings slates available.{' '}
          {isToday
            ? 'Check back later when slates are released.'
            : 'Try selecting a different date.'}
        </p>
        <div className="max-w-sm mx-auto bg-ticker-card border border-ticker-border rounded-lg p-4 text-left">
          <div className="text-[10px] font-semibold text-ticker-muted mb-2">
            Roster ({previewSlots.length} slots — {sport.toUpperCase()})
          </div>
          <div className="space-y-1">
            {previewSlots.map((slot, i) => (
              <div
                key={`${slot}-${i}`}
                className="flex items-center gap-2 px-2 py-1.5 rounded bg-ticker-bg/40 border border-ticker-border/40"
              >
                <span className={`px-1.5 py-0.5 rounded text-[9px] font-bold leading-none ${
                  isDK
                    ? 'bg-green-600/15 text-green-400'
                    : 'bg-blue-600/15 text-blue-400'
                }`}>
                  {slot}
                </span>
                <span className="text-[11px] text-ticker-muted/60">— empty —</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    )
  }

  const strategyLabel = STRATEGIES.find((s) => s.key === strategy)?.label || strategy

  return (
    <div className="space-y-4">
      {/* ── Slate Tabs ──────────────────────────────────── */}
      {slates.length > 1 && (
        <div className="flex items-center gap-1 bg-ticker-card border border-ticker-border rounded-lg p-1">
          {slates.map((s) => {
            const isActive = s.name === currentSlateName
            const isLive = s.is_live || false
            const lateSwapActive = s.late_swap_active || false
            const Icon = isLive ? Radio : (SLATE_ICONS[s.name] || Tv)
            const activeColor = isLive
              ? 'bg-red-600 text-white'
              : (SLATE_ACTIVE_COLORS[s.name] || 'bg-ticker-green text-white')
            return (
              <button
                key={s.name}
                onClick={() => setActiveSlate(s.name)}
                className={`flex items-center gap-2 px-4 py-2 text-xs font-semibold rounded-md
                  transition-colors flex-1 justify-center ${
                    isActive
                      ? activeColor
                      : isLive
                        ? 'text-red-400 hover:text-red-300 hover:bg-red-900/20'
                        : 'text-ticker-muted hover:text-white hover:bg-ticker-bg'
                  }`}
              >
                <Icon className={`w-3.5 h-3.5 ${isLive && isActive ? 'animate-pulse' : ''}`} />
                {isLive ? 'Live' : s.name}
                {lateSwapActive && (
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                    isActive ? 'bg-white/20' : 'bg-red-900/30 text-red-400'
                  }`}>
                    Late Swap
                  </span>
                )}
                <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                  isActive ? 'bg-white/20' : 'bg-ticker-border'
                }`}>
                  {s.games.length}
                </span>
              </button>
            )
          })}
        </div>
      )}

      {/* ── Action Bar ──────────────────────────────────── */}
      <LineupActionBar
        platform={platform}
        setPlatform={setPlatform}
        isDK={isDK}
        sport={sport}
        strategy={strategy}
        setStrategy={setStrategy}
        contestType={contestType}
        setContestType={setContestType}
        mode={mode}
        setMode={setMode}
        showdownGameId={showdownGameId}
        setShowdownGameId={setShowdownGameId}
        numLineups={numLineups}
        setNumLineups={setNumLineups}
        maxExposure={maxExposure}
        setMaxExposure={setMaxExposure}
        recentWeight={recentWeight}
        setRecentWeight={setRecentWeight}
        optimalityThreshold={optimalityThreshold}
        setOptimalityThreshold={setOptimalityThreshold}
        lockedPlayers={lockedPlayers}
        excludedPlayers={excludedPlayers}
        availableGames={availableGames}
        deterministicMode={deterministicMode}
        setDeterministicMode={setDeterministicMode}
        enableStacking={enableStacking}
        setEnableStacking={setEnableStacking}
        primaryStackSize={primaryStackSize}
        setPrimaryStackSize={setPrimaryStackSize}
        secondaryStackSize={secondaryStackSize}
        setSecondaryStackSize={setSecondaryStackSize}
        requireBringBack={requireBringBack}
        setRequireBringBack={setRequireBringBack}
        salaryFloorPct={salaryFloorPct}
        setSalaryFloorPct={setSalaryFloorPct}
        optimizing={optimizing}
        analyzing={analyzing}
        refining={refining}
        poolLoading={poolLoading}
        draftGroupId={draftGroupId}
        lineups={lineups}
        analysis={analysis}
        onGenerate={handleGenerate}
        onAnalyze={handleAnalyze}
        onRefine={handleRefine}
        onClear={handleClear}
        onExport={handleExport}
        onDownloadCSV={handleDownloadCSV}
        onDkUploadOpen={() => setDkUploadOpen(true)}
        onDkExporterOpen={() => setDkExporterOpen(true)}
        copied={copied}
        slateMismatch={slateMismatch}
        lineupSlateName={lineupSlateName}
        lineupDraftGroupId={lineupDraftGroupId}
        currentSlateName={currentSlateName}
        isLateSwap={isLateSwap}
        pool={pool}
      />

      {/* ── Generation State (progress bars + banners) ── */}
      <LineupGenerationState
        poolLoading={poolLoading}
        pool={pool}
        poolProgress={poolProgress}
        platform={platform}
        optimizing={optimizing}
        numLineups={numLineups}
        strategyLabel={strategyLabel}
        optimizeSteps={optimizeSteps}
        analyzing={analyzing}
        lineups={lineups}
        analyzeSteps={analyzeSteps}
        refining={refining}
        refineSteps={refineSteps}
        lastRefineResult={lastRefineResult}
        setLastRefineResult={setLastRefineResult}
        slateMismatch={slateMismatch}
        lineupSlateName={lineupSlateName}
        currentSlateName={currentSlateName}
        lineupDraftGroupId={lineupDraftGroupId}
        draftGroupId={draftGroupId}
        optimizeError={optimizeError}
        setOptimizeError={setOptimizeError}
      />

      {/* ── Main Content ────────────────────────────────── */}
      {(!poolLoading || pool.length > 0) && (
        <>
          {/* Multi-lineup: Grid + Analysis (full-width) */}
          {lineups.length > 1 ? (
            <div className="space-y-4">
              <LineupGrid
                lineups={lineups}
                platform={platform}
                sport={sport}
                selectedIndex={selectedLineupIdx}
                onSelect={setSelectedLineupIdx}
                analysis={analysis}
                refinementResults={lastRefineResult?.refinement_results}
                baselineScore={baselineScore}
                baselineLineup={baselineLineup}
                sortConfig={gridSortConfig}
                onSortChange={setGridSortConfig}
              />

              <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
                {/* Selected lineup detail + analysis */}
                <div className="xl:col-span-1 space-y-4">
                  {selectedLineup && (
                    <LineupDisplay lineup={selectedLineup} platform={platform} sport={sport} baselineScore={baselineScore} />
                  )}
                  {analysis && (
                    <LineupAnalysisPanel
                      analysis={analysis}
                      selectedIndex={selectedLineupIdx}
                      platform={platform}
                      lineups={lineups}
                      draftGroupId={draftGroupId}
                      gameDate={selectedDate}
                      sport={sport}
                    />
                  )}
                </div>

                {/* Player Pool */}
                <div className="xl:col-span-2">
                  <LineupPlayerPool
                    pool={displayPool}
                    excludedPool={excludedPool}
                    lockedPlayers={lockedPlayers}
                    excludedPlayers={excludedPlayers}
                    lineupPlayerIds={lineupPlayerIds}
                    platform={platform}
                    sport={sport}
                    onToggleLock={toggleLock}
                    onToggleExclude={toggleExclude}
                    onProjectionEdit={handleProjectionEdit}
                    projectionOverrides={projectionOverrides}
                    onResetPlayer={handleResetPlayer}
                    onResetAll={handleResetAll}
                    hasEdits={hasOverrides}
                    onRefreshPool={handleRefreshPool}
                  />
                </div>
              </div>
            </div>
          ) : (
            /* Single lineup or empty: Original 1/3 + 2/3 layout */
            <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
              {/* Lineup Display (1/3 width on xl) */}
              <div className="xl:col-span-1 space-y-4">
                {lineups.length === 1 ? (
                  <>
                    <LineupDisplay lineup={lineups[0]} platform={platform} sport={sport} />
                    {analysis && (
                      <LineupAnalysisPanel
                        analysis={analysis}
                        selectedIndex={0}
                        platform={platform}
                        lineups={lineups}
                        draftGroupId={draftGroupId}
                        sport={sport}
                        gameDate={selectedDate}
                      />
                    )}
                  </>
                ) : (
                  <div className="bg-ticker-card border border-ticker-border rounded-lg p-6">
                    <div className="text-center mb-4">
                      <Zap className={`w-10 h-10 mx-auto mb-2 ${isDK ? 'text-green-600/30' : 'text-blue-600/30'}`} />
                      <p className="text-sm text-ticker-muted mb-1">
                        No lineup generated yet
                      </p>
                      <p className="text-xs text-ticker-muted">
                        Click <strong className="text-white">Generate</strong> to optimize your lineup
                      </p>
                    </div>
                    {/* Sport-aware empty roster preview — shows the slot
                        shape so the user can see at a glance that NBA = 8
                        slots, NFL = 9, MLB = 10, etc. */}
                    <div className="mt-4 pt-4 border-t border-ticker-border">
                      <div className="text-[10px] font-semibold text-ticker-muted mb-2">
                        Roster ({getDkRosterSlots(sport).length} slots — {sport.toUpperCase()})
                      </div>
                      <div className="space-y-1">
                        {getDkRosterSlots(sport).map((slot, i) => (
                          <div
                            key={`${slot}-${i}`}
                            className="flex items-center gap-2 px-2 py-1.5 rounded bg-ticker-bg/40 border border-ticker-border/40"
                          >
                            <span className={`px-1.5 py-0.5 rounded text-[9px] font-bold leading-none ${
                              isDK
                                ? 'bg-green-600/15 text-green-400'
                                : 'bg-blue-600/15 text-blue-400'
                            }`}>
                              {slot}
                            </span>
                            <span className="text-[11px] text-ticker-muted/60">— empty —</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* Player Pool (2/3 width on xl) */}
              <div className="xl:col-span-2">
                <LineupPlayerPool
                  pool={displayPool}
                  excludedPool={excludedPool}
                  lockedPlayers={lockedPlayers}
                  excludedPlayers={excludedPlayers}
                  lineupPlayerIds={lineupPlayerIds}
                  platform={platform}
                  sport={sport}
                  onToggleLock={toggleLock}
                  onToggleExclude={toggleExclude}
                  onProjectionEdit={handleProjectionEdit}
                  projectionOverrides={projectionOverrides}
                  onResetPlayer={handleResetPlayer}
                  onResetAll={handleResetAll}
                  hasEdits={hasOverrides}
                  onRefreshPool={handleRefreshPool}
                />
              </div>
            </div>
          )}
        </>
      )}

      {/* ── DK Upload Modal ──────────────────────────────── */}
      <DKUploadModal
        isOpen={dkUploadOpen}
        onClose={() => setDkUploadOpen(false)}
        lineups={lineups}
        draftGroupId={lineupDraftGroupId || draftGroupId}
        sport={sport}
        slateName={currentSlateName}
      />

      {/* ── DK Exporter Modal ─────────────────────────────── */}
      <DKExporterModal
        isOpen={dkExporterOpen}
        onClose={() => setDkExporterOpen(false)}
        lineups={lineups}
        sport={sport}
      />
    </div>
  )
})

export default LineupBuilder
