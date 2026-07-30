/**
 * LineupGenerationState — Progress bars, banners, and status indicators
 * shown during pool loading, lineup generation, analysis, and refinement.
 *
 * Includes:
 * - Pool loading progress bar
 * - Optimization progress bar
 * - Analysis progress bar
 * - Refinement progress bar
 * - Refinement results banner
 * - Slate mismatch warning
 * - Optimization error banner
 */

import React from 'react'
import { Sparkles, XCircle } from 'lucide-react'
import ProgressBar from './ProgressBar'

export default function LineupGenerationState({
  poolLoading, pool, poolProgress, platform,
  optimizing, numLineups, strategyLabel,
  optimizeSteps,
  analyzing, lineups, analyzeSteps,
  refining, refineSteps,
  lastRefineResult, setLastRefineResult,
  slateMismatch, lineupSlateName, currentSlateName,
  lineupDraftGroupId, draftGroupId,
  optimizeError, setOptimizeError,
}) {
  return (
    <>
      {/* Pool Loading */}
      {poolLoading && !pool.length && (
        <ProgressBar
          active
          label="Loading Player Pool"
          sublabel={
            poolProgress
              ? `${poolProgress.step} (${poolProgress.completed}/${poolProgress.total} teams)`
              : 'Building player pool...'
          }
          progress={
            poolProgress && poolProgress.total > 0
              ? Math.round((poolProgress.completed / poolProgress.total) * 100)
              : null
          }
          platform={platform}
          estimatedMs={poolProgress ? poolProgress.total * 4000 : 30000}
          showTimer
        />
      )}

      {/* Optimization Progress */}
      {optimizing && (
        <ProgressBar
          active
          label={numLineups > 1 ? `Generating ${numLineups} Lineups` : 'Optimizing Lineup'}
          sublabel={
            numLineups > 1
              ? `Strategy: ${strategyLabel}`
              : 'Building optimal roster...'
          }
          steps={optimizeSteps}
          platform={platform}
          estimatedMs={numLineups > 1 ? numLineups * 3000 + 5000 : 5000}
          showTimer
        />
      )}

      {/* Analysis Progress */}
      {analyzing && (
        <ProgressBar
          active
          label={`Analyzing ${lineups.length} Lineup${lineups.length > 1 ? 's' : ''}`}
          sublabel="Evaluating quality, risks, and swap opportunities..."
          steps={analyzeSteps}
          platform={platform}
          estimatedMs={lineups.length * 1500 + 2000}
          showTimer
        />
      )}

      {/* Refinement Progress */}
      {refining && (
        <ProgressBar
          active
          label={`Refining ${lineups.length} Lineup${lineups.length > 1 ? 's' : ''}`}
          sublabel="Applying swap improvements to increase grades..."
          steps={refineSteps}
          platform={platform}
          estimatedMs={lineups.length * 2000 + 4000}
          showTimer
        />
      )}

      {/* Refinement Results Banner */}
      {lastRefineResult && !refining && (
        <div className={`rounded-lg border px-4 py-3 flex items-center gap-3 ${
          lastRefineResult.total_swaps_applied > 0
            ? 'bg-yellow-600/10 border-yellow-500/30'
            : 'bg-ticker-card border-ticker-border'
        }`}>
          <Sparkles className={`w-5 h-5 flex-shrink-0 ${
            lastRefineResult.total_swaps_applied > 0 ? 'text-yellow-400' : 'text-ticker-muted'
          }`} />
          <div className="flex-1">
            <p className="text-sm font-semibold text-white">
              {lastRefineResult.total_swaps_applied > 0
                ? `Applied ${lastRefineResult.total_swaps_applied} swap${lastRefineResult.total_swaps_applied > 1 ? 's' : ''}`
                : 'No improvements found'}
            </p>
            <p className="text-xs text-ticker-muted">
              {lastRefineResult.total_swaps_applied > 0
                ? `Avg score improved by +${lastRefineResult.avg_score_improvement.toFixed(1)} points`
                : 'Lineups are already at their best with current pool'}
              {' · '}
              {lastRefineResult.refinement_results?.map((r, i) => (
                <span key={i} className="inline-flex items-center gap-0.5 mr-2">
                  <span className="text-ticker-muted">#{i + 1}:</span>
                  <span className={r.refined_score > r.original_score ? 'text-green-400' : 'text-ticker-muted'}>
                    {r.original_grade} → {r.refined_grade}
                  </span>
                </span>
              ))}
            </p>
          </div>
          <button
            onClick={() => setLastRefineResult(null)}
            className="text-xs text-ticker-muted hover:text-white"
          >
            ✕
          </button>
        </div>
      )}

      {/* Slate Mismatch Warning */}
      {slateMismatch && lineups.length > 0 && (
        <div className="rounded-lg border border-orange-500/50 bg-orange-600/10 px-4 py-3 flex items-center gap-3">
          <span className="text-orange-400 text-lg flex-shrink-0">⚠️</span>
          <div className="flex-1">
            <p className="text-sm font-semibold text-orange-300">
              Slate Mismatch — Lineups are for {lineupSlateName}, not {currentSlateName}
            </p>
            <p className="text-xs text-orange-300/70">
              Your {lineups.length} lineup{lineups.length > 1 ? 's' : ''} use player IDs from the{' '}
              <strong>{lineupSlateName}</strong> slate (DG {lineupDraftGroupId}).
              Exporting to a <strong>{currentSlateName}</strong> contest (DG {draftGroupId}) will cause
              "not eligible" errors on DraftKings. Switch back to <strong>{lineupSlateName}</strong> or
              re-generate lineups for <strong>{currentSlateName}</strong>.
            </p>
          </div>
        </div>
      )}

      {/* Optimization Error Banner */}
      {optimizeError && (
        <div className="rounded-lg bg-red-600/10 border border-red-500/30 px-4 py-3 flex items-start gap-3">
          <XCircle className="w-5 h-5 text-red-400 flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <p className="text-sm text-red-300 font-semibold">Generation Failed</p>
            <p className="text-xs text-red-300/70 mt-1">{optimizeError}</p>
          </div>
          <button
            onClick={() => setOptimizeError(null)}
            className="text-red-400/60 hover:text-red-300 text-xs"
          >
            Dismiss
          </button>
        </div>
      )}
    </>
  )
}
