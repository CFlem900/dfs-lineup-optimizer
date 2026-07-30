/**
 * Date utilities for consistent local-timezone date handling.
 *
 * IMPORTANT: Never use `new Date().toISOString().split('T')[0]` to get
 * "today" — that returns the UTC date, which is wrong after ~6 PM CST
 * (midnight UTC).  Use `getLocalToday()` instead.
 */

/**
 * Get today's date as a YYYY-MM-DD string in the user's local timezone.
 *
 * At 9:40 PM CST on Feb 20, this correctly returns "2026-02-20"
 * (whereas `.toISOString()` would return "2026-02-21" because UTC
 * has already rolled over to the next day).
 */
export function getLocalToday() {
  const d = new Date()
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}
