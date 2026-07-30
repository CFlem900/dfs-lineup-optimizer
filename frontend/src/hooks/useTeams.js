/**
 * useTeams — TanStack Query hook for fetching the team list.
 *
 * Replaces the manual fetchTeams + useState + useEffect in App.jsx.
 * Teams rarely change, so staleTime is set high (30 min).
 */

import { useQuery } from '@tanstack/react-query'

const API_BASE = '/api'

export default function useTeams(sport) {
  return useQuery({
    queryKey: ['teams', sport],
    queryFn: async () => {
      const params = sport !== 'nba' ? `?sport=${sport}` : ''
      const res = await fetch(`${API_BASE}/teams${params}`, { credentials: 'include' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      return data.teams
    },
    staleTime: 30 * 60 * 1000, // 30 min — team list rarely changes
    gcTime: 60 * 60 * 1000,    // 60 min
  })
}
