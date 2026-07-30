import { useState, useEffect, useCallback, useRef } from 'react'

const API_BASE = '/api'
const POLL_INTERVAL = 90_000 // 90 seconds — staggered from news panel's 60s

/**
 * Custom hook for polling expert signals with auto-refresh.
 * Pauses polling when the browser tab is hidden.
 *
 * @param {string|null} teamAbbr - Team abbreviation to filter by
 * @param {string[]|null} playerNames - Player names to filter by
 * @param {number} limit - Max signals to fetch
 * @returns {{ signals, sentimentSummary, expertsTracked, loading, error, refresh }}
 */
export default function useExpertSignals(teamAbbr, playerNames, limit = 30, sport = 'nba') {
  const [signals, setSignals] = useState([])
  const [sentimentSummary, setSentimentSummary] = useState({})
  const [expertsTracked, setExpertsTracked] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const fetchRef = useRef(0)
  const timerRef = useRef(null)

  const fetchSignals = useCallback(async () => {
    const fetchId = ++fetchRef.current
    setError(null)

    try {
      const params = new URLSearchParams()
      if (teamAbbr) params.set('team_abbr', teamAbbr)
      if (playerNames && playerNames.length > 0) {
        params.set('player_names', playerNames.join(','))
      }
      params.set('limit', String(limit))
      if (sport) params.set('sport', sport)

      const res = await fetch(`${API_BASE}/expert-signals?${params.toString()}`, { credentials: 'include' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()

      if (fetchId === fetchRef.current) {
        setSignals(data.signals || [])
        setSentimentSummary(data.sentiment_summary || {})
        setExpertsTracked(data.experts_tracked || 0)
      }
    } catch (err) {
      if (fetchId === fetchRef.current) {
        setError('Failed to load expert signals')
        setSignals([])
      }
    } finally {
      if (fetchId === fetchRef.current) {
        setLoading(false)
      }
    }
  }, [teamAbbr, playerNames?.join(','), limit, sport]) // eslint-disable-line react-hooks/exhaustive-deps

  // Initial fetch + auto-refresh (paused when tab hidden)
  useEffect(() => {
    setLoading(true)
    fetchSignals()

    const tick = () => {
      if (document.hidden) return
      fetchSignals()
    }
    timerRef.current = setInterval(tick, POLL_INTERVAL)

    const onVisibility = () => {
      if (!document.hidden) fetchSignals()
    }
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [fetchSignals])

  const refresh = useCallback(() => {
    setLoading(true)
    fetchSignals()
  }, [fetchSignals])

  return { signals, sentimentSummary, expertsTracked, loading, error, refresh }
}
