import React, { useState, useEffect, useCallback, useRef } from 'react'
import {
  Upload,
  BarChart3,
  RefreshCw,
  Trash2,
  ChevronDown,
  ChevronUp,
  AlertCircle,
  CheckCircle2,
  Loader2,
  FileText,
  X,
} from 'lucide-react'
import { rotationAPI } from '../services/api'
import { getLocalToday } from '../utils/dateUtils'

/**
 * TournamentPanel — Import DK contest CSVs, run analysis, view calibrations.
 *
 * Supports multi-file selection: pick one or many CSVs and import them
 * sequentially with per-file progress feedback.
 *
 * Sits in the left sidebar alongside ExpertPanel and ChatPanel.
 */
export default function TournamentPanel({ sport = 'nba' }) {
  const [expanded, setExpanded] = useState(false)
  const [calibrations, setCalibrations] = useState([])
  const [mergedCount, setMergedCount] = useState(0)

  // Import state
  const [importFiles, setImportFiles] = useState([])
  const [contestDate, setContestDate] = useState(getLocalToday())
  const [contestType, setContestType] = useState('gpp')
  const [importing, setImporting] = useState(false)
  const [importProgress, setImportProgress] = useState(null) // { current, total, fileName }
  const [importResults, setImportResults] = useState([])
  const fileInputRef = useRef(null)

  // Analysis state
  const [analyzing, setAnalyzing] = useState(false)
  const [analysisResult, setAnalysisResult] = useState(null)

  // Status
  const [error, setError] = useState(null)

  const loadCalibrations = useCallback(async () => {
    try {
      const data = await rotationAPI.getTournamentCalibrations()
      setCalibrations(data.calibrations || [])
      setMergedCount(data.total_count || 0)
    } catch (e) {
      // Non-fatal — calibrations may not exist yet
    }
  }, [])

  useEffect(() => {
    if (expanded) loadCalibrations()
  }, [expanded, loadCalibrations])

  const handleFileChange = (e) => {
    const files = Array.from(e.target.files || [])
    setImportFiles(files)
    setImportResults([])
    setError(null)
  }

  const removeFile = (index) => {
    setImportFiles((prev) => prev.filter((_, i) => i !== index))
  }

  const handleImport = async () => {
    if (importFiles.length === 0) return
    setImporting(true)
    setError(null)
    setImportResults([])
    setImportProgress(null)

    const results = []
    let errorCount = 0

    if (importFiles.length > 1) {
      // Batch import — single request for all files
      setImportProgress({ current: 1, total: 1, fileName: `${importFiles.length} files (batch)` })
      try {
        const batchResult = await rotationAPI.importTournamentBatch({
          files: importFiles,
          contestType,
        })
        // Convert batch response into per-file results
        for (const r of (batchResult.results || [])) {
          results.push({ file: r.file, success: true, message: r.message })
        }
        for (const e of (batchResult.errors || [])) {
          results.push({ file: e.file, success: false, message: e.error })
          errorCount++
        }
      } catch (e) {
        // Fallback: if batch endpoint fails, try one-by-one
        for (let i = 0; i < importFiles.length; i++) {
          const file = importFiles[i]
          setImportProgress({ current: i + 1, total: importFiles.length, fileName: file.name })
          try {
            const result = await rotationAPI.importTournamentCSV({
              file,
              contestDate,
              contestName: file.name.replace(/\.csv$/i, ''),
              contestType,
            })
            results.push({ file: file.name, success: true, message: result.message })
          } catch (err) {
            results.push({ file: file.name, success: false, message: err.message })
            errorCount++
          }
        }
      }
    } else {
      // Single file — use original endpoint
      const file = importFiles[0]
      setImportProgress({ current: 1, total: 1, fileName: file.name })
      try {
        const result = await rotationAPI.importTournamentCSV({
          file,
          contestDate,
          contestName: file.name.replace(/\.csv$/i, ''),
          contestType,
        })
        results.push({ file: file.name, success: true, message: result.message })
      } catch (e) {
        results.push({ file: file.name, success: false, message: e.message })
        errorCount++
      }
    }

    setImportResults(results)
    setImportProgress(null)
    setImportFiles([])
    if (fileInputRef.current) fileInputRef.current.value = ''
    setImporting(false)

    if (errorCount > 0 && errorCount === importFiles.length) {
      setError(`All ${errorCount} file(s) failed to import`)
    } else if (errorCount > 0) {
      setError(`${errorCount} of ${importFiles.length} file(s) had errors`)
    }
  }

  const handleAnalyze = async () => {
    setAnalyzing(true)
    setError(null)
    setAnalysisResult(null)
    try {
      const data = await rotationAPI.getTournamentAnalysis()
      setAnalysisResult(data)
      await loadCalibrations()
    } catch (e) {
      setError(e.message)
    } finally {
      setAnalyzing(false)
    }
  }

  const handleReset = async () => {
    setError(null)
    try {
      await rotationAPI.resetTournamentCalibrations({})
      setCalibrations([])
      setMergedCount(0)
      setAnalysisResult(null)
      setImportResults([])
    } catch (e) {
      setError(e.message)
    }
  }

  return (
    <div className="bg-market-card border border-market-border rounded-lg overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-market-muted hover:text-ticker-text transition-colors"
      >
        <span className="flex items-center gap-1.5">
          <BarChart3 size={13} />
          Tournament ML
          {mergedCount > 0 && (
            <span className="ml-1 px-1.5 py-0.5 bg-ticker-green/10 text-ticker-green rounded text-[10px]">
              {mergedCount} active
            </span>
          )}
        </span>
        {expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
      </button>

      {expanded && (
        <div className="px-3 pb-3 space-y-3 border-t border-market-border">
          {/* CSV Import Section */}
          <div className="pt-2">
            <label className="block text-[10px] font-medium text-market-muted uppercase tracking-wider mb-1">
              Import Contest CSVs
            </label>
            <div className="space-y-1.5">
              <input
                ref={fileInputRef}
                type="file"
                accept=".csv"
                multiple
                onChange={handleFileChange}
                className="w-full text-[11px] text-market-muted file:mr-2 file:py-1 file:px-2 file:rounded file:border-0 file:text-[10px] file:font-medium file:bg-market-border file:text-ticker-text hover:file:bg-market-hover cursor-pointer"
              />

              {/* Selected file list */}
              {importFiles.length > 1 && (
                <div className="space-y-0.5 max-h-24 overflow-y-auto">
                  {importFiles.map((file, idx) => (
                    <div
                      key={`${file.name}-${idx}`}
                      className="flex items-center justify-between px-2 py-0.5 rounded bg-market-bg text-[10px]"
                    >
                      <span className="flex items-center gap-1 text-ticker-text/80 truncate mr-1">
                        <FileText size={9} className="shrink-0" />
                        {file.name}
                      </span>
                      <button
                        onClick={() => removeFile(idx)}
                        className="text-market-muted hover:text-ticker-red transition-colors shrink-0"
                      >
                        <X size={9} />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              <div className="flex gap-1.5">
                <input
                  type="date"
                  value={contestDate}
                  onChange={(e) => setContestDate(e.target.value)}
                  className="flex-1 px-2 py-1 text-[11px] bg-market-bg border border-market-border rounded text-ticker-text"
                />
                <select
                  value={contestType}
                  onChange={(e) => setContestType(e.target.value)}
                  className="px-2 py-1 text-[11px] bg-market-bg border border-market-border rounded text-ticker-text"
                >
                  <option value="gpp">GPP</option>
                  <option value="cash">Cash</option>
                  <option value="single_entry">Single Entry</option>
                </select>
              </div>
              <button
                onClick={handleImport}
                disabled={importFiles.length === 0 || importing}
                className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-[11px] font-medium rounded bg-ticker-blue/20 text-ticker-blue hover:bg-ticker-blue/30 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                {importing ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <Upload size={12} />
                )}
                {importing
                  ? `Importing ${importProgress?.current || ''}/${importProgress?.total || ''}...`
                  : importFiles.length > 1
                    ? `Import ${importFiles.length} CSVs`
                    : 'Import CSV'}
              </button>
            </div>

            {/* Import progress */}
            {importing && importProgress && (
              <div className="mt-1.5 space-y-1">
                <div className="w-full bg-market-bg rounded-full h-1">
                  <div
                    className="bg-ticker-blue h-1 rounded-full transition-all duration-300"
                    style={{ width: `${(importProgress.current / importProgress.total) * 100}%` }}
                  />
                </div>
                <p className="text-[10px] text-market-muted truncate">
                  {importProgress.fileName}
                </p>
              </div>
            )}

            {/* Import results */}
            {importResults.length > 0 && (
              <div className="mt-1.5 space-y-0.5 max-h-28 overflow-y-auto">
                {importResults.map((r, idx) => (
                  <div
                    key={idx}
                    className={`flex items-start gap-1.5 text-[10px] ${
                      r.success ? 'text-ticker-green' : 'text-ticker-red'
                    }`}
                  >
                    {r.success ? (
                      <CheckCircle2 size={10} className="mt-0.5 shrink-0" />
                    ) : (
                      <AlertCircle size={10} className="mt-0.5 shrink-0" />
                    )}
                    <span className="leading-tight">
                      <span className="font-medium">{r.file}:</span> {r.message}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Run Analysis */}
          <div>
            <button
              onClick={handleAnalyze}
              disabled={analyzing}
              className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-[11px] font-medium rounded bg-ticker-green/20 text-ticker-green hover:bg-ticker-green/30 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              {analyzing ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <BarChart3 size={12} />
              )}
              {analyzing ? 'Analyzing patterns...' : 'Run Pattern Analysis'}
            </button>

            {analysisResult?.analysis && (
              <div className="mt-2 space-y-1.5 text-[10px]">
                <div className="text-market-muted">
                  {analysisResult.analysis.patterns?.length || 0} patterns found
                </div>
                {analysisResult.analysis.reasoning && (
                  <p className="text-ticker-text/80 leading-relaxed">
                    {analysisResult.analysis.reasoning}
                  </p>
                )}
              </div>
            )}
            {analysisResult?.message && !analysisResult.analysis && (
              <p className="mt-1.5 text-[10px] text-market-muted">
                {analysisResult.message}
              </p>
            )}
          </div>

          {/* Active Calibrations */}
          {calibrations.length > 0 && (
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] font-medium text-market-muted uppercase tracking-wider">
                  Active Calibrations
                </span>
                <button
                  onClick={loadCalibrations}
                  className="text-market-muted hover:text-ticker-text transition-colors"
                >
                  <RefreshCw size={10} />
                </button>
              </div>
              <div className="space-y-0.5 max-h-40 overflow-y-auto">
                {calibrations.map((cal) => {
                  const isBoost = cal.adjustment_value > 1.0
                  const pct = ((cal.adjustment_value - 1.0) * 100).toFixed(1)
                  return (
                    <div
                      key={cal.calibration_key}
                      className="flex items-center justify-between px-2 py-1 rounded bg-market-bg text-[10px]"
                    >
                      <span className="text-ticker-text/80 truncate mr-2">
                        {cal.calibration_key}
                      </span>
                      <span
                        className={`font-mono shrink-0 ${
                          isBoost ? 'text-ticker-green' : 'text-ticker-red'
                        }`}
                      >
                        {isBoost ? '+' : ''}{pct}%
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Reset Button */}
          <button
            onClick={handleReset}
            className="w-full flex items-center justify-center gap-1.5 px-2 py-1 text-[10px] font-medium rounded text-ticker-red/70 hover:text-ticker-red hover:bg-ticker-red/10 transition-colors"
          >
            <Trash2 size={10} />
            Reset All Calibrations
          </button>

          {/* Error */}
          {error && (
            <div className="flex items-start gap-1.5 text-[10px] text-ticker-red">
              <AlertCircle size={11} className="mt-0.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
