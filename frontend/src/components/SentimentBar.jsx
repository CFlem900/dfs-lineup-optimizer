import React from 'react'

/**
 * Horizontal sentiment bar showing aggregate expert consensus.
 * Green = bullish, Red = bearish, Yellow = neutral.
 */
function SentimentBar({ sentimentSummary }) {
  const bullish = Math.round((sentimentSummary.bullish || 0) * 100)
  const bearish = Math.round((sentimentSummary.bearish || 0) * 100)
  const neutral = Math.round((sentimentSummary.neutral || 0) * 100)

  // Determine overall consensus
  let consensusLabel = 'MIXED'
  let consensusColor = 'text-market-yellow'
  if (bullish > bearish && bullish > neutral) {
    consensusLabel = 'BULLISH'
    consensusColor = 'text-market-green'
  } else if (bearish > bullish && bearish > neutral) {
    consensusLabel = 'BEARISH'
    consensusColor = 'text-market-red'
  }

  const dominant = Math.max(bullish, bearish, neutral)

  return (
    <div className="px-3 py-2 border-b border-market-border/50">
      {/* Consensus label */}
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[9px] font-bold tracking-wider text-market-dim uppercase">
          Consensus
        </span>
        <span className={`text-[10px] font-bold ${consensusColor}`}>
          {dominant}% {consensusLabel}
        </span>
      </div>

      {/* Stacked horizontal bar */}
      <div className="flex h-1.5 rounded-full overflow-hidden bg-market-border/30">
        {bullish > 0 && (
          <div
            className="bg-market-green/80 transition-all duration-700"
            style={{ width: `${bullish}%` }}
            title={`${bullish}% Bullish`}
          />
        )}
        {neutral > 0 && (
          <div
            className="bg-market-yellow/60 transition-all duration-700"
            style={{ width: `${neutral}%` }}
            title={`${neutral}% Neutral`}
          />
        )}
        {bearish > 0 && (
          <div
            className="bg-market-red/70 transition-all duration-700"
            style={{ width: `${bearish}%` }}
            title={`${bearish}% Bearish`}
          />
        )}
      </div>

      {/* Legend */}
      <div className="flex items-center gap-3 mt-1">
        <span className="flex items-center gap-1 text-[8px] text-market-dim">
          <span className="w-1.5 h-1.5 rounded-full bg-market-green/80" />
          {bullish}%
        </span>
        <span className="flex items-center gap-1 text-[8px] text-market-dim">
          <span className="w-1.5 h-1.5 rounded-full bg-market-yellow/60" />
          {neutral}%
        </span>
        <span className="flex items-center gap-1 text-[8px] text-market-dim">
          <span className="w-1.5 h-1.5 rounded-full bg-market-red/70" />
          {bearish}%
        </span>
      </div>
    </div>
  )
}

export default SentimentBar
