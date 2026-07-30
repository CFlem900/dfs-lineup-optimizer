/**
 * DKUploadModal — Automated DraftKings entry upload workflow.
 *
 * 4-step modal:
 * 1. Auth Check — verify DK session cookies
 * 2. Download Template — pull entries CSV from DK
 * 3. Review & Confirm — show entry/contest summary
 * 4. Upload — push filled CSV back to DK
 */

import React, { useState, useEffect, useCallback, useRef } from 'react'
import {
  X,
  CheckCircle,
  XCircle,
  Loader2,
  Download,
  Upload,
  Rocket,
  AlertTriangle,
  ChevronRight,
} from 'lucide-react'
import { rotationAPI } from '../services/api'

const STEPS = [
  { key: 'auth', label: 'Auth Check', icon: CheckCircle },
  { key: 'download', label: 'Download', icon: Download },
  { key: 'review', label: 'Review', icon: AlertTriangle },
  { key: 'upload', label: 'Upload', icon: Upload },
]

export default function DKUploadModal({
  isOpen,
  onClose,
  lineups,
  draftGroupId,
  sport,
  slateName,
}) {
  const [currentStep, setCurrentStep] = useState(0)
  const [authResult, setAuthResult] = useState(null)
  const [template, setTemplate] = useState(null)
  const [fillResult, setFillResult] = useState(null)
  const [uploadResult, setUploadResult] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [progressDetail, setProgressDetail] = useState('')

  // Cancellation ref — prevents state updates after modal closes
  const cancelledRef = useRef(false)

  // Reset state when modal opens / cleanup when it closes
  useEffect(() => {
    if (isOpen) {
      cancelledRef.current = false
      setCurrentStep(0)
      setAuthResult(null)
      setTemplate(null)
      setFillResult(null)
      setUploadResult(null)
      setError(null)
      setLoading(false)
      setProgressDetail('')
      // Auto-run auth check
      doAuthCheck()
    } else {
      // Signal cancellation so in-flight requests don't update state
      cancelledRef.current = true
    }
  }, [isOpen]) // eslint-disable-line react-hooks/exhaustive-deps

  // Helper to guard state setters against cancelled/closed modal
  const safeSet = useCallback((setter) => (...args) => {
    if (!cancelledRef.current) setter(...args)
  }, [])

  // ── Step 1: Auth Check ─────────────────────────────────────────
  const doAuthCheck = useCallback(async () => {
    setLoading(true)
    setError(null)
    setProgressDetail('Checking DK session...')
    try {
      const result = await rotationAPI.dkAuthCheck()
      if (cancelledRef.current) return
      setAuthResult(result)
      if (result.authenticated) {
        setCurrentStep(1)
      }
    } catch (err) {
      if (cancelledRef.current) return
      setError(`Auth check failed: ${err.message}`)
    } finally {
      if (!cancelledRef.current) {
        setLoading(false)
        setProgressDetail('')
      }
    }
  }, [])

  // ── Step 2: Download Template ──────────────────────────────────
  const runDownload = useCallback(async () => {
    if (!lineups?.length) {
      setError('No lineups to upload')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const result = await rotationAPI.dkDownloadTemplate(
        { draftGroupId, sport },
        (progress) => safeSet(setProgressDetail)(progress.detail || progress.step),
      )
      if (cancelledRef.current) return
      setTemplate(result)

      // Auto-fill entries
      setProgressDetail('Filling entries with lineups...')
      const playerIds = lineups.map((lu) =>
        lu.players.map((p) => p.dk_player_id).filter(Boolean)
      )
      // Per-lineup scores feed the smart selector (cash → high floor,
      // GPP → high ceiling). If a lineup is missing fields we send 0 and
      // it falls to the bottom of the priority list.
      const lineupMeta = lineups.map((lu) => ({
        projection: lu.total_projected_fp ?? 0,
        ceiling: lu.total_ceiling_fp ?? 0,
        floor: lu.total_floor_fp ?? 0,
      }))
      const filled = await rotationAPI.dkFillEntries({
        template: result,
        lineupPlayerIds: playerIds,
        lineupMeta,
        // mode='auto' is the backend default — explicit here for clarity.
        selection: { mode: 'auto', dedupe_per_contest: true },
      })
      if (cancelledRef.current) return
      setFillResult(filled)
      setCurrentStep(2)
    } catch (err) {
      if (cancelledRef.current) return
      setError(`Download failed: ${err.message}`)
    } finally {
      if (!cancelledRef.current) {
        setLoading(false)
        setProgressDetail('')
      }
    }
  }, [draftGroupId, sport, lineups, safeSet])

  // ── Step 4: Upload ─────────────────────────────────────────────
  const runUpload = useCallback(async () => {
    if (!fillResult?.filled_csv) return
    setLoading(true)
    setError(null)
    setCurrentStep(3)
    try {
      const result = await rotationAPI.dkUploadEntries(
        fillResult.filled_csv,
        (progress) => safeSet(setProgressDetail)(progress.detail || progress.step),
      )
      if (cancelledRef.current) return
      setUploadResult(result)
    } catch (err) {
      if (cancelledRef.current) return
      setError(`Upload failed: ${err.message}`)
    } finally {
      if (!cancelledRef.current) {
        setLoading(false)
        setProgressDetail('')
      }
    }
  }, [fillResult, safeSet])

  if (!isOpen) return null

  const totalEntries = template?.entries?.length || 0
  const contestSummary = template?.contest_summary || {}
  const numContests = Object.keys(contestSummary).length

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-ticker-card border border-ticker-border rounded-xl shadow-2xl w-full max-w-lg mx-4">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-ticker-border">
          <div className="flex items-center gap-2">
            <Rocket className="w-5 h-5 text-green-400" />
            <h2 className="text-lg font-bold text-white">Upload to DraftKings</h2>
          </div>
          <button onClick={onClose} className="text-ticker-muted hover:text-white">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Step Indicator */}
        <div className="flex items-center gap-1 px-5 py-3 border-b border-ticker-border/50">
          {STEPS.map((step, idx) => {
            const Icon = step.icon
            const isActive = idx === currentStep
            const isDone = idx < currentStep
            const isFailed =
              (idx === 0 && authResult && !authResult.authenticated) ||
              (idx === 3 && uploadResult && !uploadResult.success)
            return (
              <React.Fragment key={step.key}>
                {idx > 0 && (
                  <ChevronRight className={`w-3 h-3 flex-shrink-0 ${
                    isDone ? 'text-green-400' : 'text-ticker-border'
                  }`} />
                )}
                <div className={`flex items-center gap-1.5 px-2 py-1 rounded text-xs font-semibold ${
                  isActive
                    ? 'bg-green-600/20 text-green-400'
                    : isDone
                      ? 'text-green-400'
                      : isFailed
                        ? 'text-red-400'
                        : 'text-ticker-muted'
                }`}>
                  {isDone ? (
                    <CheckCircle className="w-3.5 h-3.5" />
                  ) : isFailed ? (
                    <XCircle className="w-3.5 h-3.5" />
                  ) : isActive && loading ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  ) : (
                    <Icon className="w-3.5 h-3.5" />
                  )}
                  {step.label}
                </div>
              </React.Fragment>
            )
          })}
        </div>

        {/* Content */}
        <div className="px-5 py-4 min-h-[200px]">
          {/* Error banner */}
          {error && (
            <div className="mb-4 rounded-lg bg-red-600/10 border border-red-500/30 px-4 py-3 flex items-start gap-3">
              <XCircle className="w-5 h-5 text-red-400 flex-shrink-0 mt-0.5" />
              <div>
                <p className="text-sm text-red-300 font-semibold">Error</p>
                <p className="text-xs text-red-300/70 mt-1">{error}</p>
              </div>
            </div>
          )}

          {/* Progress indicator */}
          {loading && progressDetail && (
            <div className="mb-4 flex items-center gap-2 text-sm text-ticker-muted">
              <Loader2 className="w-4 h-4 animate-spin text-green-400" />
              {progressDetail}
            </div>
          )}

          {/* Step 0: Auth Check */}
          {currentStep === 0 && !loading && (
            <div className="space-y-4">
              {authResult?.authenticated ? (
                <div className="flex items-center gap-3 text-green-400">
                  <CheckCircle className="w-6 h-6" />
                  <div>
                    <p className="text-sm font-semibold">Authenticated</p>
                    <p className="text-xs text-ticker-muted">
                      Cookies: {authResult.cookies_found?.join(', ') || 'found'}
                    </p>
                  </div>
                </div>
              ) : authResult ? (
                <div className="space-y-3">
                  <div className="flex items-center gap-3 text-red-400">
                    <XCircle className="w-6 h-6" />
                    <div>
                      <p className="text-sm font-semibold">Not Authenticated</p>
                      <p className="text-xs text-red-300/70">
                        {authResult.error || 'DK session not found'}
                      </p>
                    </div>
                  </div>
                  <div className="bg-ticker-bg rounded-lg p-3 text-xs text-ticker-muted space-y-1">
                    <p className="font-semibold text-white">To fix:</p>
                    {authResult.error?.toLowerCase().includes('close chrome') ? (
                      <>
                        <p>1. Close Chrome completely (check system tray)</p>
                        <p>2. Come back here and click "Retry"</p>
                        <p>3. Reopen Chrome after auth succeeds</p>
                        <p className="text-yellow-400 mt-2">
                          Or: Run the backend server as Administrator to read cookies while Chrome is open.
                        </p>
                      </>
                    ) : (
                      <>
                        <p>1. Open Chrome and go to draftkings.com</p>
                        <p>2. Log into your DraftKings account</p>
                        <p>3. Close Chrome, then click "Retry"</p>
                      </>
                    )}
                  </div>
                  <button
                    onClick={doAuthCheck}
                    className="px-4 py-2 text-xs font-semibold bg-green-600 hover:bg-green-500 text-white rounded"
                  >
                    Retry Auth Check
                  </button>
                </div>
              ) : null}
            </div>
          )}

          {/* Step 1: Download Template */}
          {currentStep === 1 && !loading && (
            <div className="space-y-4">
              <div className="bg-ticker-bg rounded-lg p-4 space-y-2">
                <p className="text-sm text-white font-semibold">
                  Ready to download entries for {slateName || 'this slate'}
                </p>
                <p className="text-xs text-ticker-muted">
                  Draft Group: {draftGroupId} &middot; Sport: {sport?.toUpperCase()} &middot;{' '}
                  {lineups.length} lineup{lineups.length !== 1 ? 's' : ''} to upload
                </p>
              </div>
              <button
                onClick={runDownload}
                disabled={loading}
                className="flex items-center gap-2 px-4 py-2 text-xs font-bold bg-green-600 hover:bg-green-500 text-white rounded disabled:opacity-40"
              >
                <Download className="w-4 h-4" />
                Download Entries from DK
              </button>
            </div>
          )}

          {/* Step 2: Review & Confirm */}
          {currentStep === 2 && !loading && (
            <div className="space-y-4">
              {/* Template summary */}
              <div className="bg-ticker-bg rounded-lg p-4 space-y-3">
                <p className="text-sm text-white font-semibold">
                  {totalEntries} entries across {numContests} contest{numContests !== 1 ? 's' : ''}
                </p>
                <div className="space-y-1">
                  {Object.entries(contestSummary).map(([name, count]) => (
                    <div key={name} className="flex justify-between text-xs">
                      <span className="text-ticker-muted truncate mr-2">{name}</span>
                      <span className="text-white font-semibold">{count} entries</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Fill summary */}
              {fillResult && (
                <div className="bg-ticker-bg rounded-lg p-4 space-y-2">
                  <p className="text-sm text-white font-semibold">
                    Filled {fillResult.entries_filled} entries with{' '}
                    {fillResult.lineups_used} lineup{fillResult.lineups_used !== 1 ? 's' : ''}
                  </p>
                  {fillResult.lineup_cycling && (
                    <p className="text-xs text-yellow-400">
                      Lineups cycled to fill all entries
                    </p>
                  )}
                  {fillResult.warnings?.length > 0 && (
                    <div className="space-y-1 mt-2">
                      {fillResult.warnings.map((w, i) => (
                        <p key={i} className="text-xs text-orange-400 flex items-center gap-1">
                          <AlertTriangle className="w-3 h-3 flex-shrink-0" />
                          {w}
                        </p>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Confirm button */}
              <div className="flex gap-2">
                <button
                  onClick={runUpload}
                  disabled={loading || (fillResult?.warnings?.some(w => w.includes('wrong slate')))}
                  className="flex items-center gap-2 px-4 py-2 text-xs font-bold bg-green-600 hover:bg-green-500 text-white rounded disabled:opacity-40"
                >
                  <Upload className="w-4 h-4" />
                  Confirm & Upload to DK
                </button>
                <button
                  onClick={onClose}
                  className="px-4 py-2 text-xs font-semibold border border-ticker-border text-ticker-muted hover:text-white rounded"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {/* Step 3: Upload Result */}
          {currentStep === 3 && !loading && uploadResult && (
            <div className="space-y-4">
              {uploadResult.success ? (
                <div className="flex items-center gap-3 text-green-400">
                  <CheckCircle className="w-8 h-8" />
                  <div>
                    <p className="text-sm font-bold">Upload Successful!</p>
                    <p className="text-xs text-ticker-muted">
                      {uploadResult.entries_updated > 0
                        ? `${uploadResult.entries_updated} entries updated on DraftKings`
                        : uploadResult.message || 'Entries submitted successfully'}
                    </p>
                  </div>
                </div>
              ) : (
                <div className="space-y-3">
                  <div className="flex items-center gap-3 text-red-400">
                    <XCircle className="w-8 h-8" />
                    <div>
                      <p className="text-sm font-bold">Upload Failed</p>
                      <p className="text-xs text-red-300/70">
                        {uploadResult.errors?.join('; ') || 'Unknown error'}
                      </p>
                    </div>
                  </div>
                  <button
                    onClick={runUpload}
                    className="px-4 py-2 text-xs font-semibold bg-green-600 hover:bg-green-500 text-white rounded"
                  >
                    Retry Upload
                  </button>
                </div>
              )}
              <button
                onClick={onClose}
                className="px-4 py-2 text-xs font-semibold border border-ticker-border text-ticker-muted hover:text-white rounded"
              >
                Close
              </button>
            </div>
          )}
        </div>

        {/* Footer info */}
        <div className="px-5 py-3 border-t border-ticker-border/50">
          <p className="text-[10px] text-ticker-muted">
            Uses Playwright browser automation with your Chrome DK session cookies.
            No credentials are stored or transmitted.
          </p>
        </div>
      </div>
    </div>
  )
}
