/**
 * DKExporterModal — Fill DraftKings entries CSV with generated lineups.
 *
 * 3-step modal:
 * 1. Upload — drag-and-drop / file input for DKEntries.csv, auto-parse
 * 2. Review & Fill — contest/entry table with per-entry lineup assignment
 * 3. Download — filled CSV blob download
 */

import React, { useState, useEffect, useRef, useMemo } from 'react'
import {
  X,
  Upload,
  FileSpreadsheet,
  Download,
  CheckCircle,
  AlertTriangle,
  Loader2,
  ChevronDown,
  ChevronRight,
  RotateCcw,
} from 'lucide-react'
import { rotationAPI } from '../services/api'

const STEPS = [
  { key: 'upload', label: 'Upload', icon: Upload },
  { key: 'review', label: 'Review & Fill', icon: FileSpreadsheet },
  { key: 'download', label: 'Download', icon: Download },
]

export default function DKExporterModal({ isOpen, onClose, lineups, sport }) {
  // ── Internal state ──────────────────────────────────────────────
  const [currentStep, setCurrentStep] = useState(0)
  const [file, setFile] = useState(null)
  const [parseResult, setParseResult] = useState(null)
  const [parsing, setParsing] = useState(false)
  const [allowDuplicates, setAllowDuplicates] = useState(false)
  const [assignments, setAssignments] = useState({}) // entry_id -> lineup index
  const [expandedContests, setExpandedContests] = useState({}) // contest_id -> bool
  const [filling, setFilling] = useState(false)
  const [fillResult, setFillResult] = useState(null) // { blob, meta }
  const [error, setError] = useState(null)
  const [isDragging, setIsDragging] = useState(false)

  const fileRef = useRef(null)
  const cancelledRef = useRef(false)

  // ── Reset on open/close ────────────────────────────────────────
  useEffect(() => {
    if (isOpen) {
      cancelledRef.current = false
      setCurrentStep(0)
      setFile(null)
      setParseResult(null)
      setParsing(false)
      setAllowDuplicates(false)
      setAssignments({})
      setExpandedContests({})
      setFilling(false)
      setFillResult(null)
      setError(null)
      setIsDragging(false)
    } else {
      cancelledRef.current = true
    }
  }, [isOpen])

  // ── Derived data ───────────────────────────────────────────────
  const allEntries = useMemo(() => {
    if (!parseResult?.contests) return []
    return parseResult.contests.flatMap((c) =>
      c.entries.map((e) => ({ ...e, contest_name: c.contest_name, contest_id: c.contest_id, entry_fee: c.entry_fee }))
    )
  }, [parseResult])

  const totalEntries = parseResult?.total_entries || 0
  const totalContests = parseResult?.total_contests || 0
  const numLineups = lineups?.length || 0
  const isCycling = totalEntries > numLineups && numLineups > 0

  // ── File handling ──────────────────────────────────────────────
  const handleFileSelect = async (selectedFile) => {
    if (!selectedFile) return
    if (!selectedFile.name.toLowerCase().endsWith('.csv')) {
      setError('Please select a CSV file')
      return
    }

    setFile(selectedFile)
    setError(null)
    setParsing(true)

    try {
      const result = await rotationAPI.parseEntriesCSV(selectedFile)
      if (cancelledRef.current) return

      setParseResult(result)

      // Build default round-robin assignments
      const entries = result.contests.flatMap((c) => c.entries)
      const defaultAssignments = {}
      entries.forEach((e, i) => {
        defaultAssignments[e.entry_id] = numLineups > 0 ? i % numLineups : 0
      })
      setAssignments(defaultAssignments)

      // Expand first contest by default
      if (result.contests.length > 0) {
        setExpandedContests({ [result.contests[0].contest_id]: true })
      }

      setCurrentStep(1)
    } catch (err) {
      if (cancelledRef.current) return
      setError(`Parse failed: ${err.message}`)
    } finally {
      if (!cancelledRef.current) setParsing(false)
    }
  }

  const handleFileInput = (e) => {
    const f = e.target.files?.[0]
    if (f) handleFileSelect(f)
  }

  // ── Drag-and-drop ─────────────────────────────────────────────
  const handleDragOver = (e) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(true)
  }

  const handleDragLeave = (e) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(false)
  }

  const handleDrop = (e) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDragging(false)
    const f = e.dataTransfer.files?.[0]
    if (f) handleFileSelect(f)
  }

  // ── Fill action ───────────────────────────────────────────────
  const handleFill = async () => {
    if (!file || !parseResult || numLineups === 0) return

    setFilling(true)
    setError(null)

    try {
      // Build the lineups array in the order needed per entry
      // Each entry's assigned lineup index -> extract players
      const entries = allEntries
      const mappedLineups = entries.map((entry) => {
        const idx = assignments[entry.entry_id] ?? 0
        const lu = lineups[idx]
        if (!lu?.players) return []
        return lu.players.map((p) => ({
          dk_player_id: p.dk_player_id,
          display_name: p.display_name || p.player_name || '',
        }))
      })

      const result = await rotationAPI.fillEntriesCSV({
        file,
        lineups: mappedLineups,
        allowDuplicates,
      })
      if (cancelledRef.current) return

      setFillResult(result)
      setCurrentStep(2)
    } catch (err) {
      if (cancelledRef.current) return
      setError(`Fill failed: ${err.message}`)
    } finally {
      if (!cancelledRef.current) setFilling(false)
    }
  }

  // ── Download ──────────────────────────────────────────────────
  const handleDownload = () => {
    if (!fillResult?.blob) return
    const url = URL.createObjectURL(fillResult.blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'DKEntries_filled.csv'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  // ── Start over ────────────────────────────────────────────────
  const handleStartOver = () => {
    setCurrentStep(0)
    setFile(null)
    setParseResult(null)
    setAssignments({})
    setExpandedContests({})
    setFillResult(null)
    setError(null)
    if (fileRef.current) fileRef.current.value = ''
  }

  // ── Contest toggle ────────────────────────────────────────────
  const toggleContest = (contestId) => {
    setExpandedContests((prev) => ({ ...prev, [contestId]: !prev[contestId] }))
  }

  if (!isOpen) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-ticker-card border border-ticker-border rounded-xl shadow-2xl w-full max-w-2xl mx-4 max-h-[85vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-ticker-border shrink-0">
          <div className="flex items-center gap-2">
            <FileSpreadsheet className="w-5 h-5 text-emerald-400" />
            <h2 className="text-lg font-bold text-white">DK Entries Exporter</h2>
          </div>
          <button onClick={onClose} className="text-ticker-muted hover:text-white transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Step Indicator */}
        <div className="flex items-center gap-1 px-5 py-3 border-b border-ticker-border/50 shrink-0">
          {STEPS.map((step, idx) => {
            const Icon = step.icon
            const isActive = idx === currentStep
            const isDone = idx < currentStep
            return (
              <React.Fragment key={step.key}>
                {idx > 0 && (
                  <ChevronRight
                    className={`w-3 h-3 flex-shrink-0 ${isDone ? 'text-emerald-400' : 'text-ticker-border'}`}
                  />
                )}
                <div
                  className={`flex items-center gap-1.5 px-2 py-1 rounded text-xs font-semibold ${
                    isActive
                      ? 'bg-emerald-600/20 text-emerald-400'
                      : isDone
                        ? 'text-emerald-400'
                        : 'text-ticker-muted'
                  }`}
                >
                  {isDone ? (
                    <CheckCircle className="w-3.5 h-3.5" />
                  ) : isActive && (parsing || filling) ? (
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
        <div className="px-5 py-4 overflow-y-auto flex-1 min-h-0">
          {/* Error banner */}
          {error && (
            <div className="mb-4 rounded-lg bg-red-600/10 border border-red-500/30 px-4 py-3 flex items-start gap-3">
              <AlertTriangle className="w-5 h-5 text-red-400 flex-shrink-0 mt-0.5" />
              <div className="flex-1">
                <p className="text-sm text-red-300 font-semibold">Error</p>
                <p className="text-xs text-red-300/70 mt-1">{error}</p>
              </div>
            </div>
          )}

          {/* ── Step 0: Upload ────────────────────────────────── */}
          {currentStep === 0 && (
            <div className="space-y-4">
              <div className="bg-ticker-bg rounded-lg p-4">
                <p className="text-sm text-white font-semibold mb-1">Upload DraftKings Entries CSV</p>
                <p className="text-xs text-ticker-muted">
                  Download your entries CSV from{' '}
                  <span className="text-emerald-400">draftkings.com/lineup/upload</span> and upload it here to fill
                  with your generated lineups.
                </p>
              </div>

              {/* Drop zone */}
              <div
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                onClick={() => fileRef.current?.click()}
                className={`relative border-2 border-dashed rounded-xl p-10 text-center cursor-pointer transition-all ${
                  isDragging
                    ? 'border-emerald-400 bg-emerald-600/10'
                    : 'border-ticker-border hover:border-ticker-muted hover:bg-ticker-bg/50'
                }`}
              >
                {parsing ? (
                  <div className="flex flex-col items-center gap-3">
                    <Loader2 className="w-10 h-10 text-emerald-400 animate-spin" />
                    <p className="text-sm text-ticker-muted">Parsing CSV...</p>
                  </div>
                ) : (
                  <div className="flex flex-col items-center gap-3">
                    <Upload
                      className={`w-10 h-10 ${isDragging ? 'text-emerald-400' : 'text-ticker-muted'}`}
                    />
                    <div>
                      <p className="text-sm text-white font-semibold">
                        {isDragging ? 'Drop CSV here' : 'Drag & drop your DKEntries.csv'}
                      </p>
                      <p className="text-xs text-ticker-muted mt-1">or click to browse</p>
                    </div>
                  </div>
                )}
                <input
                  ref={fileRef}
                  type="file"
                  accept=".csv"
                  onChange={handleFileInput}
                  className="hidden"
                />
              </div>

              {/* Lineup count info */}
              <div className="text-xs text-ticker-muted">
                {numLineups > 0 ? (
                  <span>
                    <span className="text-emerald-400 font-semibold">{numLineups}</span> lineup
                    {numLineups !== 1 ? 's' : ''} ready to fill
                  </span>
                ) : (
                  <span className="text-orange-400">
                    <AlertTriangle className="w-3 h-3 inline mr-1" />
                    Generate lineups first before filling entries
                  </span>
                )}
              </div>
            </div>
          )}

          {/* ── Step 1: Review & Fill ─────────────────────────── */}
          {currentStep === 1 && parseResult && (
            <div className="space-y-4">
              {/* Summary bar */}
              <div className="bg-ticker-bg rounded-lg p-4 flex items-center justify-between">
                <div>
                  <p className="text-sm text-white font-semibold">
                    {totalEntries} entries across {totalContests} contest{totalContests !== 1 ? 's' : ''}
                  </p>
                  <p className="text-xs text-ticker-muted mt-0.5">
                    Roster: {parseResult.roster_slots?.join(', ')} &middot; File: {file?.name}
                  </p>
                </div>
                <button
                  onClick={handleStartOver}
                  className="flex items-center gap-1 text-xs text-ticker-muted hover:text-white transition-colors"
                  title="Upload a different file"
                >
                  <RotateCcw className="w-3 h-3" />
                  Re-upload
                </button>
              </div>

              {/* Cycling warning */}
              {isCycling && !allowDuplicates && (
                <div className="rounded-lg bg-orange-600/10 border border-orange-500/30 px-4 py-3 flex items-start gap-3">
                  <AlertTriangle className="w-4 h-4 text-orange-400 flex-shrink-0 mt-0.5" />
                  <p className="text-xs text-orange-300">
                    {totalEntries} entries but only {numLineups} lineup{numLineups !== 1 ? 's' : ''} — lineups will
                    be cycled (reused). Enable <strong>Allow Duplicates</strong> to suppress this warning.
                  </p>
                </div>
              )}

              {/* Contest accordion */}
              <div className="space-y-2">
                {parseResult.contests.map((contest) => {
                  const isExpanded = expandedContests[contest.contest_id] ?? false
                  return (
                    <div
                      key={contest.contest_id}
                      className="border border-ticker-border rounded-lg overflow-hidden"
                    >
                      {/* Contest header */}
                      <button
                        onClick={() => toggleContest(contest.contest_id)}
                        className="w-full flex items-center justify-between px-4 py-2.5 bg-ticker-bg hover:bg-ticker-bg/80 transition-colors"
                      >
                        <div className="flex items-center gap-2 text-left min-w-0">
                          {isExpanded ? (
                            <ChevronDown className="w-3.5 h-3.5 text-ticker-muted shrink-0" />
                          ) : (
                            <ChevronRight className="w-3.5 h-3.5 text-ticker-muted shrink-0" />
                          )}
                          <span className="text-xs font-semibold text-white truncate">
                            {contest.contest_name}
                          </span>
                          <span className="text-[10px] text-ticker-muted shrink-0">
                            {contest.entry_fee}
                          </span>
                        </div>
                        <span className="text-[10px] font-semibold text-emerald-400 shrink-0 ml-2">
                          {contest.entries.length} entr{contest.entries.length !== 1 ? 'ies' : 'y'}
                        </span>
                      </button>

                      {/* Entry rows */}
                      {isExpanded && (
                        <div className="border-t border-ticker-border/50">
                          <table className="w-full text-xs">
                            <thead>
                              <tr className="text-[10px] text-ticker-muted uppercase tracking-wider">
                                <th className="text-left px-4 py-1.5 font-medium">Entry ID</th>
                                <th className="text-left px-4 py-1.5 font-medium">Current Roster</th>
                                <th className="text-right px-4 py-1.5 font-medium">Assigned Lineup</th>
                              </tr>
                            </thead>
                            <tbody>
                              {contest.entries.map((entry) => {
                                const filledSlots = Object.values(entry.current_roster || {}).filter(Boolean).length
                                const totalSlots = Object.keys(entry.current_roster || {}).length
                                return (
                                  <tr
                                    key={entry.entry_id}
                                    className="border-t border-ticker-border/30 hover:bg-ticker-bg/30"
                                  >
                                    <td className="px-4 py-1.5 text-ticker-text font-mono">
                                      {entry.entry_id}
                                    </td>
                                    <td className="px-4 py-1.5 text-ticker-muted">
                                      {filledSlots > 0 ? (
                                        <span>
                                          {filledSlots}/{totalSlots} filled
                                        </span>
                                      ) : (
                                        <span className="text-ticker-muted/50">empty</span>
                                      )}
                                    </td>
                                    <td className="px-4 py-1.5 text-right">
                                      <select
                                        value={assignments[entry.entry_id] ?? 0}
                                        onChange={(e) =>
                                          setAssignments((prev) => ({
                                            ...prev,
                                            [entry.entry_id]: parseInt(e.target.value, 10),
                                          }))
                                        }
                                        className="bg-ticker-bg border border-ticker-border rounded px-2 py-0.5 text-[11px] text-ticker-text"
                                      >
                                        {lineups.map((lu, idx) => (
                                          <option key={idx} value={idx}>
                                            L{idx + 1}{' '}
                                            ({lu.total_projected_fp?.toFixed(1) ?? '?'} pts)
                                          </option>
                                        ))}
                                      </select>
                                    </td>
                                  </tr>
                                )
                              })}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>

              {/* Controls */}
              <div className="flex items-center justify-between pt-2">
                <label className="flex items-center gap-2 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={allowDuplicates}
                    onChange={(e) => setAllowDuplicates(e.target.checked)}
                    className="rounded border-ticker-border bg-ticker-bg text-emerald-500 focus:ring-emerald-500/30"
                  />
                  <span className="text-xs text-ticker-muted">Allow Duplicate Lineups</span>
                </label>

                <button
                  onClick={handleFill}
                  disabled={filling || numLineups === 0}
                  className="flex items-center gap-2 px-4 py-2 text-xs font-bold bg-emerald-600 hover:bg-emerald-500 text-white rounded disabled:opacity-40 transition-colors"
                >
                  {filling ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <FileSpreadsheet className="w-4 h-4" />
                  )}
                  {filling ? 'Filling entries...' : 'Auto-Fill Entries'}
                </button>
              </div>
            </div>
          )}

          {/* ── Step 2: Download ──────────────────────────────── */}
          {currentStep === 2 && fillResult && (
            <div className="space-y-4">
              {/* Success summary */}
              <div className="flex items-center gap-3 text-emerald-400">
                <CheckCircle className="w-8 h-8" />
                <div>
                  <p className="text-sm font-bold">Entries Filled Successfully</p>
                  <p className="text-xs text-ticker-muted">
                    {fillResult.meta.entriesFilled} entries filled with{' '}
                    {fillResult.meta.lineupsUsed} lineup{fillResult.meta.lineupsUsed !== 1 ? 's' : ''}
                  </p>
                </div>
              </div>

              {/* Contest breakdown */}
              {Object.keys(fillResult.meta.contestSummary).length > 0 && (
                <div className="bg-ticker-bg rounded-lg p-4 space-y-1">
                  {Object.entries(fillResult.meta.contestSummary).map(([name, count]) => (
                    <div key={name} className="flex justify-between text-xs">
                      <span className="text-ticker-muted truncate mr-2">{name}</span>
                      <span className="text-white font-semibold shrink-0">{count} entries</span>
                    </div>
                  ))}
                </div>
              )}

              {/* Warnings */}
              {fillResult.meta.warnings?.length > 0 && (
                <div className="space-y-1">
                  {fillResult.meta.warnings.map((w, i) => (
                    <p key={i} className="text-xs text-orange-400 flex items-center gap-1">
                      <AlertTriangle className="w-3 h-3 flex-shrink-0" />
                      {w}
                    </p>
                  ))}
                </div>
              )}

              {/* Download button */}
              <div className="flex gap-2 pt-2">
                <button
                  onClick={handleDownload}
                  className="flex items-center gap-2 px-5 py-2.5 text-sm font-bold bg-emerald-600 hover:bg-emerald-500 text-white rounded transition-colors"
                >
                  <Download className="w-5 h-5" />
                  Download DK CSV
                </button>
                <button
                  onClick={handleStartOver}
                  className="flex items-center gap-1.5 px-4 py-2 text-xs font-semibold border border-ticker-border text-ticker-muted hover:text-white rounded transition-colors"
                >
                  <RotateCcw className="w-3.5 h-3.5" />
                  Fill Another
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-5 py-3 border-t border-ticker-border/50 shrink-0">
          <p className="text-[10px] text-ticker-muted">
            Upload your DK entries CSV, fill with optimizer lineups, and re-upload to DraftKings.
            Only roster slot columns are modified — all other data is preserved exactly.
          </p>
        </div>
      </div>
    </div>
  )
}
