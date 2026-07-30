/**
 * ImportProjectionsButton — uploads a CSV with external consensus
 * projections (Player, Projection columns) to override pool projections.
 *
 * Self-contained: owns its own state (status / message / unmatched list),
 * its own file-input ref, and renders the post-upload
 * ``UnmatchedProjectionsModal`` for manual matching of any unresolved
 * names. Originally inlined in ``LineupActionBar.jsx`` (Prompt 2.3);
 * promoted to a shared component in Prompt 7.12 so the slate-page
 * Player Pool panel can offer the same affordance without duplicating
 * logic.
 *
 * The upload is **sport-scoped** via the ``sport`` prop — the backend
 * routes the import to that sport's player pool + alias bucket, so an
 * MLB upload won't bleed into the NFL pool and vice versa.
 *
 * After a successful upload the React Query ``playerPool`` cache is
 * invalidated so projections show up in the UI without a manual refresh.
 *
 * Props
 * -----
 *   isPrimaryColored : when true, the button uses platform colors
 *                      (DK green / FD blue). Optional — defaults to a
 *                      neutral border so it can sit next to the existing
 *                      DK/FD toggle on the slate panel without colliding.
 *   sport            : sport code routed to the backend (default "nba").
 *   compact          : when true, shrinks padding for tight headers
 *                      (the slate panel uses this; the lineup action
 *                      bar uses the default size).
 */

import React, { useRef, useState } from 'react'
import { RefreshCw, Upload } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { rotationAPI } from '../services/api'
import UnmatchedProjectionsModal from './UnmatchedProjectionsModal'


export default function ImportProjectionsButton({
  sport = 'nba',
  compact = false,
}) {
  const fileRef = useRef(null)
  const [status, setStatus] = useState(null) // null | 'uploading' | 'done' | 'error'
  const [message, setMessage] = useState('')
  const [unmatched, setUnmatched] = useState([])
  const [modalOpen, setModalOpen] = useState(false)
  const queryClient = useQueryClient()

  const handleFile = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    setStatus('uploading')
    setMessage('')
    try {
      // Sport-scoped import — backend routes to the correct pool and
      // alias bucket. NFL uploads will not collide with MLB players,
      // and "Cowboys" auto-resolves to "Dallas Cowboys DST" on NFL.
      const result = await rotationAPI.importProjections(file, sport)
      // Backend cleared its pool cache; force the React Query pool to
      // refetch so the new projections show up in the UI.
      await queryClient.refetchQueries({ queryKey: ['playerPool'] })
      const um = result.unmatched || []
      setUnmatched(um)
      setStatus('done')
      if (um.length > 0) {
        setMessage(`${result.imported} ok · ${um.length} unmatched`)
        setModalOpen(true)
      } else {
        const dstSuffix = result.dst_auto_resolved
          ? ` (${result.dst_auto_resolved} DST)`
          : ''
        setMessage(`${result.imported} players${dstSuffix}`)
        setTimeout(() => setStatus(null), 4000)
      }
    } catch (err) {
      setStatus('error')
      setMessage(err.message || 'Upload failed')
      setTimeout(() => setStatus(null), 5000)
    }
    // Reset file input so same file can be re-uploaded
    if (fileRef.current) fileRef.current.value = ''
  }

  // Padding tunable for tight headers (compact) vs the standard
  // action-bar size.
  const padding = compact ? 'px-2 py-1' : 'px-3 py-1.5'

  return (
    <>
      <input
        ref={fileRef}
        type="file"
        accept=".csv"
        className="hidden"
        onChange={handleFile}
      />
      <button
        onClick={() => {
          // If we have an unresolved unmatched list, re-open the modal
          // instead of triggering a new upload.
          if (unmatched.length > 0 && !modalOpen) {
            setModalOpen(true)
            return
          }
          fileRef.current?.click()
        }}
        disabled={status === 'uploading'}
        title={
          unmatched.length > 0
            ? `${unmatched.length} CSV row(s) need manual matching — click to resolve`
            : `Import external projections CSV for ${sport.toUpperCase()} ` +
              `(Player, Projection columns)`
        }
        className={`flex items-center gap-1.5 ${padding} text-xs font-semibold border rounded transition-colors
          disabled:opacity-40 ${
            status === 'done' && unmatched.length > 0
              ? 'border-yellow-500/60 text-yellow-400 hover:bg-yellow-600/10'
              : status === 'done'
              ? 'border-green-500/50 text-green-400'
              : status === 'error'
              ? 'border-red-500/50 text-red-400'
              : 'border-ticker-border text-ticker-muted hover:text-white hover:bg-ticker-bg'
          }`}
      >
        {status === 'uploading' ? (
          <RefreshCw className="w-3.5 h-3.5 animate-spin" />
        ) : (
          <Upload className="w-3.5 h-3.5" />
        )}
        {status === 'done' ? message : status === 'error' ? 'Error' : 'Import Proj'}
      </button>

      <UnmatchedProjectionsModal
        isOpen={modalOpen}
        unmatched={unmatched}
        sport={sport}
        onClose={() => setModalOpen(false)}
        onSaved={async () => {
          // After saving aliases the backend re-keyed live imports;
          // refetch the pool so the freshly-resolved players show
          // their projections.
          await queryClient.refetchQueries({ queryKey: ['playerPool'] })
        }}
      />
    </>
  )
}
