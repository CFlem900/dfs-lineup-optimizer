/**
 * useScoreboard — TanStack Query hook for fetching slate/scoreboard data.
 *
 * Replaces the manual fetchSlate + useState + useEffect in App.jsx.
 * Scoreboard data updates frequently near game time, so staleTime is low (2 min).
 */

import { useQuery } from '@tanstack/react-query'

const API_BASE = '/api'

export default function useScoreboard(selectedDate, sport) {
  return useQuery({
    queryKey: ['scoreboard', selectedDate, sport],
    queryFn: async () => {
      const params = new URLSearchParams()
      if (selectedDate) params.set('game_date', selectedDate)
      if (sport !== 'nba') params.set('sport', sport)
      const qs = params.toString()
      const res = await fetch(`${API_BASE}/scoreboard${qs ? `?${qs}` : ''}`, { credentials: 'include' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.json()
    },
    staleTime: 2 * 60 * 1000, // 2 min — scoreboard updates near game time
    gcTime: 10 * 60 * 1000,   // 10 min
  })
}
