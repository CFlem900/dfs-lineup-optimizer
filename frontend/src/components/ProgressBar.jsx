/**
 * ProgressBar — Animated progress indicator with step labels.
 *
 * Supports two modes:
 * - Determinate: pass `progress` (0-100) for a real percentage bar
 * - Indeterminate: pass `indeterminate={true}` for a pulsing/shimmer animation
 *
 * Includes optional step labels, elapsed timer, and platform-aware coloring.
 */

import React, { useState, useEffect, useRef } from 'react'

// Simulated progress curve: starts fast, slows near the end
function simulateProgress(elapsedMs, estimatedMs) {
  const t = Math.min(elapsedMs / estimatedMs, 1)
  // Ease-out curve: rapid at start, asymptotic toward 95%
  return Math.min(95, 100 * (1 - Math.pow(1 - t, 2.5)))
}

export default function ProgressBar({
  active = false,
  progress = null,        // 0-100, or null for simulated/indeterminate
  steps = [],             // [{ label, done }]
  label = '',             // Main label text
  sublabel = '',          // Secondary detail text
  platform = 'dk',
  estimatedMs = 10000,    // Estimated total time (for simulated progress)
  showTimer = true,
}) {
  const isDK = platform === 'dk'
  const [simulated, setSimulated] = useState(0)
  const [elapsed, setElapsed] = useState(0)
  const startRef = useRef(null)
  const frameRef = useRef(null)

  // Simulated progress animation
  useEffect(() => {
    if (!active) {
      setSimulated(0)
      setElapsed(0)
      startRef.current = null
      if (frameRef.current) cancelAnimationFrame(frameRef.current)
      return
    }

    startRef.current = Date.now()

    function tick() {
      const now = Date.now()
      const ms = now - startRef.current
      setElapsed(ms)
      setSimulated(simulateProgress(ms, estimatedMs))
      frameRef.current = requestAnimationFrame(tick)
    }

    frameRef.current = requestAnimationFrame(tick)
    return () => {
      if (frameRef.current) cancelAnimationFrame(frameRef.current)
    }
  }, [active, estimatedMs])

  if (!active) return null

  const pct = progress !== null ? progress : simulated
  const elapsedSec = Math.floor(elapsed / 1000)

  // Completed steps count
  const doneCount = steps.filter((s) => s.done).length
  const currentStep = steps.find((s) => !s.done)

  const barColor = isDK ? 'bg-green-500' : 'bg-blue-500'
  const barGlow = isDK ? 'shadow-green-500/30' : 'shadow-blue-500/30'
  const accentText = isDK ? 'text-green-400' : 'text-blue-400'

  return (
    <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden">
      {/* Header label */}
      <div className="px-4 pt-3 pb-1 flex items-center justify-between">
        <div className="flex items-center gap-2">
          {/* Pulsing dot */}
          <span className="relative flex h-2.5 w-2.5">
            <span
              className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${
                isDK ? 'bg-green-400' : 'bg-blue-400'
              }`}
            />
            <span
              className={`relative inline-flex rounded-full h-2.5 w-2.5 ${
                isDK ? 'bg-green-500' : 'bg-blue-500'
              }`}
            />
          </span>
          <span className="text-xs font-semibold text-white">{label}</span>
        </div>
        <div className="flex items-center gap-3 text-[10px] text-ticker-muted">
          {showTimer && elapsedSec > 0 && <span>{elapsedSec}s</span>}
          <span className={`font-mono font-bold ${accentText}`}>
            {Math.round(pct)}%
          </span>
        </div>
      </div>

      {/* Progress bar */}
      <div className="px-4 pb-2">
        <div className="w-full bg-ticker-bg rounded-full h-2 overflow-hidden">
          <div
            className={`h-2 rounded-full transition-all duration-300 ease-out ${barColor} shadow-lg ${barGlow}`}
            style={{ width: `${pct}%` }}
          >
            {/* Shimmer effect */}
            <div className="w-full h-full relative overflow-hidden rounded-full">
              <div
                className="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent animate-shimmer"
                style={{
                  animation: 'shimmer 1.5s ease-in-out infinite',
                }}
              />
            </div>
          </div>
        </div>
      </div>

      {/* Sublabel / current step */}
      {(sublabel || currentStep || steps.length > 0) && (
        <div className="px-4 pb-3 flex items-center justify-between">
          <span className="text-[10px] text-ticker-muted">
            {currentStep ? currentStep.label : sublabel}
          </span>
          {steps.length > 0 && (
            <span className="text-[10px] text-ticker-muted font-mono">
              {doneCount}/{steps.length} steps
            </span>
          )}
        </div>
      )}

      {/* Step indicators */}
      {steps.length > 1 && (
        <div className="px-4 pb-3">
          <div className="flex items-center gap-1">
            {steps.map((step, i) => (
              <div key={i} className="flex-1 flex flex-col items-center gap-1">
                {/* Step dot/segment */}
                <div
                  className={`h-1 w-full rounded-full transition-all duration-500 ${
                    step.done
                      ? barColor
                      : i === doneCount
                      ? isDK
                        ? 'bg-green-500/40'
                        : 'bg-blue-500/40'
                      : 'bg-ticker-border'
                  }`}
                />
                <span
                  className={`text-[9px] truncate max-w-full ${
                    step.done
                      ? accentText
                      : i === doneCount
                      ? 'text-white'
                      : 'text-ticker-muted/50'
                  }`}
                >
                  {step.label}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
