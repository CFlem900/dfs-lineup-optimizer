/**
 * UnmatchedProjectionsModal
 *
 * After a CSV projection import, the backend reports any rows whose
 * normalized name didn't match a player in the current pool. This modal
 * lets the user pick the correct pool player for each unmatched row from
 * a fuzzy-match suggestion list, then persists that mapping as an alias.
 *
 * Saved aliases are applied automatically by the backend on subsequent
 * imports — and to the *current* import in-place, so the user just needs
 * to wait for the pool refetch to see projections appear.
 */

import React, { useEffect, useState } from 'react'
import { X, Check, RefreshCw } from 'lucide-react'
import { rotationAPI } from '../services/api'

export default function UnmatchedProjectionsModal({
  isOpen,
  unmatched,
  sport = 'nba',
  onClose,
  onSaved,
}) {
  // Map of csvNormalized -> selected player_id
  const [picks, setPicks] = useState({})
  // Map of csvNormalized -> 'idle' | 'saving' | 'saved' | 'error'
  const [rowStatus, setRowStatus] = useState({})
  const [savingAll, setSavingAll] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (isOpen) {
      // Default each row's pick to its top suggestion (if any).
      const initial = {}
      for (const r of unmatched || []) {
        if (r.suggestions?.[0]?.player_id) {
          initial[r.csv_normalized] = r.suggestions[0].player_id
        }
      }
      setPicks(initial)
      setRowStatus({})
      setError(null)
    }
  }, [isOpen, unmatched])

  if (!isOpen) return null

  const saveOne = async (row) => {
    const playerId = picks[row.csv_normalized]
    if (!playerId) return
    setRowStatus((s) => ({ ...s, [row.csv_normalized]: 'saving' }))
    try {
      // sport-scoped: alias is saved under the same sport as the import,
      // so a manual "Cowboys" → "Dallas Cowboys DST" mapping in NFL won't
      // shadow an unrelated MLB lookup of the same string.
      await rotationAPI.addProjectionAlias({
        csvName: row.csv_name,
        playerId,
        sport,
      })
      setRowStatus((s) => ({ ...s, [row.csv_normalized]: 'saved' }))
    } catch (err) {
      console.error('[Alias] Save failed:', err)
      setRowStatus((s) => ({ ...s, [row.csv_normalized]: 'error' }))
      setError(err.message || 'Save failed')
    }
  }

  const saveAll = async () => {
    setSavingAll(true)
    setError(null)
    for (const row of unmatched || []) {
      if (rowStatus[row.csv_normalized] === 'saved') continue
      if (!picks[row.csv_normalized]) continue
      await saveOne(row)
    }
    setSavingAll(false)
    if (onSaved) onSaved()
  }

  const total = unmatched?.length ?? 0
  const savedCount = Object.values(rowStatus).filter((s) => s === 'saved').length

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="relative w-full max-w-3xl max-h-[85vh] overflow-hidden rounded-lg border border-ticker-border bg-ticker-bg shadow-2xl flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-ticker-border">
          <div>
            <h2 className="text-sm font-bold text-white">
              Unmatched Projections
              <span className="ml-2 text-xs font-normal text-ticker-muted">
                {savedCount}/{total} matched
              </span>
            </h2>
            <p className="text-[11px] text-ticker-muted mt-0.5">
              These CSV rows didn't match any pool player. Pick the correct
              player to save the mapping for future imports.
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-ticker-muted hover:text-white p-1 rounded hover:bg-ticker-border/50"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-4 py-3">
          {total === 0 ? (
            <p className="text-center text-ticker-muted text-sm py-8">
              All CSV rows matched a pool player.
            </p>
          ) : (
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-ticker-bg">
                <tr className="text-left text-[10px] uppercase text-ticker-muted border-b border-ticker-border">
                  <th className="py-2 pr-3">CSV name</th>
                  <th className="py-2 pr-3">Proj</th>
                  <th className="py-2 pr-3">Match to pool player</th>
                  <th className="py-2 pr-3 w-24 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {unmatched.map((row) => {
                  const status = rowStatus[row.csv_normalized] || 'idle'
                  const selectedId = picks[row.csv_normalized] || ''
                  return (
                    <tr
                      key={row.csv_normalized}
                      className="border-b border-ticker-border/50"
                    >
                      <td className="py-2 pr-3">
                        <div className="text-white truncate" title={row.csv_name}>
                          {row.csv_name}
                        </div>
                      </td>
                      <td className="py-2 pr-3 text-ticker-muted">
                        {row.projection?.toFixed?.(1) ?? row.projection}
                      </td>
                      <td className="py-2 pr-3">
                        <select
                          value={selectedId}
                          onChange={(e) =>
                            setPicks((p) => ({
                              ...p,
                              [row.csv_normalized]: Number(e.target.value) || '',
                            }))
                          }
                          disabled={status === 'saved'}
                          className="w-full bg-ticker-bg border border-ticker-border rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-cyan-500"
                        >
                          <option value="">— Pick a player —</option>
                          {(row.suggestions || []).map((s) => (
                            <option key={s.player_id} value={s.player_id}>
                              {s.name} ({s.team || '?'}, ${s.salary || '—'})
                            </option>
                          ))}
                          {(!row.suggestions || row.suggestions.length === 0) && (
                            <option disabled>No close matches in pool</option>
                          )}
                        </select>
                      </td>
                      <td className="py-2 pr-3 text-right">
                        {status === 'saved' ? (
                          <span className="inline-flex items-center gap-1 text-[11px] text-green-400">
                            <Check className="w-3 h-3" /> Saved
                          </span>
                        ) : (
                          <button
                            onClick={() => saveOne(row)}
                            disabled={!selectedId || status === 'saving'}
                            className="px-2 py-1 text-[11px] font-semibold border border-cyan-500/50 text-cyan-400 rounded hover:bg-cyan-600/15 disabled:opacity-40"
                          >
                            {status === 'saving' ? (
                              <RefreshCw className="w-3 h-3 animate-spin" />
                            ) : status === 'error' ? (
                              'Retry'
                            ) : (
                              'Save'
                            )}
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-4 py-3 border-t border-ticker-border">
          <div className="text-[10px] text-ticker-muted">
            {error
              ? `Error: ${error}`
              : 'Saved mappings are remembered for future imports.'}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              className="px-3 py-1.5 text-xs text-ticker-muted hover:text-white border border-ticker-border rounded"
            >
              Close
            </button>
            <button
              onClick={saveAll}
              disabled={savingAll || total === 0 || savedCount === total}
              className="px-3 py-1.5 text-xs font-semibold bg-cyan-600 hover:bg-cyan-500 text-white rounded disabled:opacity-40"
            >
              {savingAll ? 'Saving…' : 'Save all matches'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
