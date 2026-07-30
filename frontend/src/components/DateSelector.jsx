import React, { useState, useEffect } from 'react'
import { Calendar, ChevronLeft, ChevronRight } from 'lucide-react'
import { getLocalToday } from '../utils/dateUtils'

/**
 * Compact date selector with prev/next arrows and a "Today" quick-jump.
 *
 * Props:
 *   selectedDate  – YYYY-MM-DD string
 *   onDateChange  – callback(newDate: string)
 */
function DateSelector({ selectedDate, onDateChange }) {
  const [now, setNow] = useState(new Date())

  // Update clock every 30s for the live time display
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 30_000)
    return () => clearInterval(id)
  }, [])

  const todayStr = getLocalToday()

  // Guard against empty/invalid selectedDate — fall back to today
  const safeDate = selectedDate && /^\d{4}-\d{2}-\d{2}$/.test(selectedDate)
    ? selectedDate
    : todayStr

  const isToday = safeDate === todayStr

  // ±7 day range
  const minDate = shiftDate(todayStr, -7)
  const maxDate = shiftDate(todayStr, 7)

  const canGoBack = safeDate > minDate
  const canGoForward = safeDate < maxDate

  const goToPrev = () => {
    if (canGoBack) onDateChange(shiftDate(safeDate, -1))
  }
  const goToNext = () => {
    if (canGoForward) onDateChange(shiftDate(safeDate, 1))
  }
  const goToToday = () => onDateChange(todayStr)

  // Format "Saturday, February 8, 2026"
  const dateObj = parseDateLocal(safeDate)
  const formattedDate = dateObj.toLocaleDateString('en-US', {
    weekday: 'long',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  })

  const timeFormatted = now.toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  })

  return (
    <div className="border-b border-ticker-border/50 bg-ticker-bg">
      <div className="max-w-[1600px] mx-auto px-4 py-2 flex items-center justify-between">
        <div className="flex items-center gap-2">
          {/* Prev arrow */}
          <button
            onClick={goToPrev}
            disabled={!canGoBack}
            className="p-1 rounded hover:bg-ticker-card transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
            aria-label="Previous day"
          >
            <ChevronLeft className="w-4 h-4 text-ticker-muted" />
          </button>

          <Calendar className="w-4 h-4 text-ticker-green" />
          <span className="text-sm font-semibold text-white">
            {formattedDate}
          </span>

          {isToday && (
            <span className="px-1.5 py-0.5 bg-ticker-green/15 text-ticker-green text-[10px] font-bold rounded-full uppercase">
              Today
            </span>
          )}

          {/* Next arrow */}
          <button
            onClick={goToNext}
            disabled={!canGoForward}
            className="p-1 rounded hover:bg-ticker-card transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
            aria-label="Next day"
          >
            <ChevronRight className="w-4 h-4 text-ticker-muted" />
          </button>

          {/* Today quick-jump */}
          {!isToday && (
            <button
              onClick={goToToday}
              className="ml-2 px-2 py-0.5 text-[10px] font-semibold uppercase rounded border border-ticker-border text-ticker-muted hover:text-white hover:border-ticker-green transition-colors"
            >
              Today
            </button>
          )}
        </div>

        {/* Live clock — only when viewing today */}
        {isToday && (
          <span className="text-xs text-ticker-muted tabular-nums">
            {timeFormatted}
          </span>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Shift a YYYY-MM-DD string by N days and return a new YYYY-MM-DD string. */
function shiftDate(dateStr, days) {
  const d = parseDateLocal(dateStr)
  d.setDate(d.getDate() + days)
  return toISODate(d)
}

/** Parse YYYY-MM-DD as a local-timezone Date (avoids UTC offset issues). */
function parseDateLocal(dateStr) {
  const [y, m, d] = dateStr.split('-').map(Number)
  return new Date(y, m - 1, d)
}

/** Format a Date to YYYY-MM-DD. */
function toISODate(d) {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

export default DateSelector
