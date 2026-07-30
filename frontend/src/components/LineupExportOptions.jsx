/**
 * LineupExportOptions — Export clipboard, CSV download, and DK Upload buttons.
 *
 * Rendered inline within the LineupActionBar action-buttons row.
 */

import React from 'react'
import { Clipboard, CheckCircle, Download, Rocket, FileSpreadsheet } from 'lucide-react'

export default function LineupExportOptions({
  lineups,
  isDK,
  copied,
  slateMismatch,
  lineupSlateName,
  lineupDraftGroupId,
  currentSlateName,
  draftGroupId,
  onExport,
  onDownloadCSV,
  onDkUploadOpen,
  onDkExporterOpen,
}) {
  return (
    <>
      <button
        onClick={onExport}
        disabled={!lineups.length}
        className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold border border-ticker-border
                   rounded hover:bg-ticker-bg transition-colors disabled:opacity-40"
      >
        {copied ? (
          <CheckCircle className="w-3.5 h-3.5 text-ticker-green" />
        ) : (
          <Clipboard className="w-3.5 h-3.5" />
        )}
        {copied ? 'Copied!' : lineups.length > 1 ? `Export ${lineups.length}` : 'Export'}
      </button>

      <button
        onClick={onDownloadCSV}
        disabled={!lineups.length}
        className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold border
                   rounded hover:bg-ticker-bg transition-colors disabled:opacity-40 ${
                     slateMismatch ? 'border-orange-500/50 text-orange-400' : 'border-ticker-border'
                   }`}
        title={slateMismatch
          ? `⚠️ Lineups are for ${lineupSlateName} (DG ${lineupDraftGroupId}), not ${currentSlateName} (DG ${draftGroupId})`
          : 'Download lineups as CSV file for DraftKings import'}
      >
        <Download className="w-3.5 h-3.5" />
        CSV {slateMismatch && '⚠️'}
      </button>

      {isDK && (
        <button
          onClick={onDkUploadOpen}
          disabled={!lineups.length || slateMismatch}
          className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold border
                     rounded transition-colors disabled:opacity-40 ${
                       lineups.length && !slateMismatch
                         ? 'border-green-500/50 text-green-400 hover:bg-green-600/10'
                         : 'border-ticker-border text-ticker-muted'
                     }`}
          title={slateMismatch
            ? 'Fix slate mismatch before uploading'
            : 'Upload lineups directly to DraftKings contests'}
        >
          <Rocket className="w-3.5 h-3.5" />
          DK Upload
        </button>
      )}

      {isDK && (
        <button
          onClick={onDkExporterOpen}
          disabled={!lineups.length}
          className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold border
                     rounded transition-colors disabled:opacity-40 ${
                       lineups.length
                         ? 'border-emerald-500/50 text-emerald-400 hover:bg-emerald-600/10'
                         : 'border-ticker-border text-ticker-muted'
                     }`}
          title="Fill your DK entries CSV with generated lineups"
        >
          <FileSpreadsheet className="w-3.5 h-3.5" />
          DK Fill
        </button>
      )}
    </>
  )
}
