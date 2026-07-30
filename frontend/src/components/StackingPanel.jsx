/**
 * StackingPanel — Dynamic per-sport stacking controls (Prompt 5.3).
 *
 * Rendered only when ``enableStacking`` is true AND the active sport
 * has a non-null ``stackingControls`` schema in ``sportShapes.js``
 * (NFL or MLB today). The shape selects the layout:
 *
 *   nfl_style → one dropdown (QB + N pass-catchers) plus a
 *               bring-back checkbox
 *   mlb_style → two dropdowns (primary stack / secondary stack)
 *               with cross-validation that prevents the user from
 *               picking a 5+4 combination (caught by the backend
 *               model_validator otherwise — Prompt 5.1).
 *
 * State binding:
 *   The component is fully controlled — values flow in via props
 *   from ``useLineupState`` and changes are reported via the
 *   matching setters. The backend payload is built in
 *   ``services/api.js#generateLineups`` so this component does
 *   not import any API code.
 */

import React from 'react'
import { getStackingControls } from '../config/sportShapes'

export default function StackingPanel({
  sport = 'nba',
  primaryStackSize,
  setPrimaryStackSize,
  secondaryStackSize,
  setSecondaryStackSize,
  requireBringBack,
  setRequireBringBack,
}) {
  const controls = getStackingControls(sport)

  // No sport-specific stacking schema → render nothing. NBA/CBB use
  // only the boolean toggle that lives one row above this panel.
  if (!controls) return null

  if (controls.type === 'nfl_style') {
    return (
      <div className="flex items-center gap-3 px-3 py-2 bg-purple-600/10 border border-purple-600/30 rounded">
        <div className="flex items-center gap-2">
          <label
            htmlFor="nfl-primary-stack"
            className="text-xs text-ticker-muted whitespace-nowrap"
          >
            {controls.primaryLabel}:
          </label>
          <select
            id="nfl-primary-stack"
            value={primaryStackSize ?? ''}
            onChange={(e) => {
              const v = e.target.value
              setPrimaryStackSize(v === '' ? null : Number(v))
            }}
            className="bg-ticker-bg border border-ticker-border rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-purple-500"
          >
            <option value="">Default</option>
            {controls.primaryOptions.map((n) => (
              <option key={n} value={n}>
                QB + {n}
              </option>
            ))}
          </select>
        </div>

        {controls.hasBringBack && (
          <label className="flex items-center gap-1.5 text-xs text-ticker-muted cursor-pointer select-none">
            <input
              type="checkbox"
              checked={requireBringBack ?? true}
              onChange={(e) => setRequireBringBack(e.target.checked)}
              className="accent-purple-500"
            />
            <span>Require bring-back</span>
          </label>
        )}
      </div>
    )
  }

  if (controls.type === 'mlb_style') {
    // Cross-dropdown validation: the backend model_validator (Prompt 5.1)
    // rejects primary + secondary > 8 with a 422. Match that rule here
    // by visually disabling secondary choices that would push the sum
    // past 8 — friendlier than letting the user submit and get an error.
    const primaryFloor = primaryStackSize ?? 5  // backend default
    const isSecondaryDisabled = (n) => n > 0 && primaryFloor + n > 8

    return (
      <div className="flex items-center gap-3 px-3 py-2 bg-purple-600/10 border border-purple-600/30 rounded">
        <div className="flex items-center gap-2">
          <label
            htmlFor="mlb-primary-stack"
            className="text-xs text-ticker-muted whitespace-nowrap"
          >
            {controls.primaryLabel}:
          </label>
          <select
            id="mlb-primary-stack"
            value={primaryStackSize ?? ''}
            onChange={(e) => {
              const v = e.target.value
              const next = v === '' ? null : Number(v)
              setPrimaryStackSize(next)
              // Snap secondary down if the new primary makes the
              // current secondary infeasible (e.g. user goes 4 → 5
              // while secondary was 4).
              if (next !== null && secondaryStackSize !== null
                && secondaryStackSize > 0
                && next + secondaryStackSize > 8) {
                setSecondaryStackSize(8 - next)
              }
            }}
            className="bg-ticker-bg border border-ticker-border rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-purple-500"
          >
            <option value="">Default</option>
            {controls.primaryOptions.map((n) => (
              <option key={n} value={n}>
                {n}-stack
              </option>
            ))}
          </select>
        </div>

        <div className="flex items-center gap-2">
          <label
            htmlFor="mlb-secondary-stack"
            className="text-xs text-ticker-muted whitespace-nowrap"
          >
            {controls.secondaryLabel}:
          </label>
          <select
            id="mlb-secondary-stack"
            value={secondaryStackSize ?? ''}
            onChange={(e) => {
              const v = e.target.value
              setSecondaryStackSize(v === '' ? null : Number(v))
            }}
            className="bg-ticker-bg border border-ticker-border rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-purple-500"
          >
            <option value="">Default</option>
            {controls.secondaryOptions.map((n) => (
              <option
                key={n}
                value={n}
                disabled={isSecondaryDisabled(n)}
                title={
                  isSecondaryDisabled(n)
                    ? `Primary ${primaryFloor} + secondary ${n} exceeds 8 hitter slots`
                    : undefined
                }
              >
                {n === 0 ? 'None' : `${n}-stack`}
              </option>
            ))}
          </select>
        </div>
      </div>
    )
  }

  // Unknown control type — render nothing rather than crash so adding
  // a new sport with a typo doesn't break the whole form.
  return null
}
