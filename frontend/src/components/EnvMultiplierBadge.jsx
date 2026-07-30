/**
 * EnvMultiplierBadge — shared ±% chip surfacing the MLB park × wind
 * adjustment that the backend folded into ``adjusted_fp``.
 *
 * Originally inlined in ``LineupPlayerPool.jsx`` (Prompt 7.2). Promoted
 * to a shared component in Prompt 7.4 so ``LineupGrid`` and
 * ``LineupDetailModal`` can reuse the exact same visual + tooltip
 * without duplicating logic — the contract from 7.2 was "user sees
 * the same chip everywhere a projection is shown," and this consolidation
 * is what makes that promise enforceable.
 *
 * The backend exposes a derived ``env_multiplier`` field on every
 * player serialization. For non-MLB sports it's ``1.0`` and we render
 * nothing, so this component is safe to drop into any sport's table
 * without sport-gating in the parent.
 *
 * Render rules:
 *   1.0   → null (no badge — keeps NBA / NFL / CBB tables clean)
 *   > 1.0 → green +X% PF chip
 *   < 1.0 → red    -X% PF chip
 *
 * Tooltip exposes the exact math (``Raw × mult = Adj``) so a curious
 * user can verify the optimizer is doing what it claims.
 *
 * The ``size`` prop lets caller tables tune the chip footprint:
 *   "xs" → 9px (compact tables: player pool, lineup grid)
 *   "sm" → 10px (default for detail modal where there's more room)
 */

import React from 'react'

export default function EnvMultiplierBadge({ player, size = 'xs' }) {
  const mult = player?.env_multiplier
  if (mult == null || mult === 1 || mult === 1.0) return null

  const raw = player.projected_fp ?? 0
  const adj = player.adjusted_fp ?? raw
  const pct = Math.round((mult - 1) * 100)
  const isPositive = mult > 1
  const sign = isPositive ? '+' : ''
  const tooltip =
    `Raw: ${raw.toFixed(1)} × ${mult.toFixed(2)} = Adj: ${adj.toFixed(1)}\n` +
    `Park/Wind adjustment`

  const sizeClass = size === 'sm'
    ? 'px-1.5 py-0.5 text-[10px]'
    : 'px-1 py-0.5 text-[9px]'

  return (
    <span
      title={tooltip}
      className={`inline-flex items-center font-bold rounded leading-none border ${sizeClass} ${
        isPositive
          ? 'bg-green-900/30 text-green-300 border-green-700/40'
          : 'bg-red-900/30 text-red-300 border-red-700/40'
      }`}
    >
      {sign}{pct}% PF
    </span>
  )
}
