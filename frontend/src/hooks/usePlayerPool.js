/**
 * usePlayerPool — TanStack Query hook for fetching the DFS player pool.
 *
 * Replaces the manual useState/useEffect/useRef pool-fetching logic
 * that was previously in useLineupState.js (~100 lines).
 *
 * Features:
 *  - SSE streaming with progress callbacks → REST fallback
 *  - 10-min stale time (matches previous POOL_CACHE_TTL)
 *  - Deduplication & stale-fetch detection handled by TanStack Query
 *  - Server-side cache preload on mount
 */

import { useState, useEffect, useRef, useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { rotationAPI } from '../services/api'
import { devLog } from '../utils/logger'

// Stable empty arrays to avoid creating new references on every render.
// Without these, `query.data?.players ?? []` creates a new [] each time
// query.data is undefined, which triggers useEffect loops downstream.
const EMPTY_PLAYERS = []
const EMPTY_EXCLUDED = []

async function fetchPlayerPool({ sport, platform, draftGroupId, gameDate, onProgress }) {
  // Strategy: REST first (reliable), SSE stream as optional enhancement.
  // EventSource through Vite's dev proxy is unreliable — the connection
  // frequently drops before the "done" event fires.  REST is fast because
  // the backend caches the built pool, so there's no performance penalty.
  try {
    const data = await rotationAPI.getPlayerPool({ platform, draftGroupId, gameDate, sport })
    const players = data.players || []
    const excludedPlayers = data.excluded_players || []
    if (players.length > 0) {
      devLog(`[Pool] REST loaded: ${players.length} players, ${excludedPlayers.length} excluded`)
      return { players, excludedPlayers }
    }
    // REST returned 0 players — pool may still be building. Try SSE stream
    // which waits for the build to complete with progress updates.
    devLog('[Pool] REST returned 0 players, trying SSE stream...')
  } catch (restErr) {
    devLog('[Pool] REST failed, trying SSE stream:', restErr.message)
  }

  // Fallback: SSE stream (handles initial pool build with progress events)
  try {
    const data = await rotationAPI.streamPlayerPool(
      { platform, draftGroupId, gameDate, sport },
      onProgress,
    )
    const players = data.players || []
    const excludedPlayers = data.excluded_players || []
    devLog(`[Pool] Stream loaded: ${players.length} players, ${excludedPlayers.length} excluded`)
    return { players, excludedPlayers }
  } catch (sseErr) {
    console.error('[Pool] Both REST and SSE failed:', sseErr.message)
    throw sseErr
  }
}

export default function usePlayerPool({ sport, platform, draftGroupId, selectedDate }) {
  const queryClient = useQueryClient()
  const [poolProgress, setPoolProgress] = useState(null)

  // Ref so the queryFn closure always sees the latest setter without
  // causing the queryFn identity to change (which would trigger refetches).
  const progressRef = useRef(setPoolProgress)
  progressRef.current = setPoolProgress

  const query = useQuery({
    queryKey: ['playerPool', sport, platform, draftGroupId, selectedDate],
    queryFn: () =>
      fetchPlayerPool({
        sport,
        platform,
        draftGroupId,
        gameDate: selectedDate,
        onProgress: (p) => progressRef.current(p),
      }),
    enabled: !!draftGroupId,
    staleTime: 10 * 60 * 1000,   // 10 min — matches previous POOL_CACHE_TTL
    gcTime: 30 * 60 * 1000,      // 30 min before garbage collection
    retry: 2,                     // Retry twice on failure (SSE can be flaky)
    retryDelay: 1000,             // 1 second between retries
    refetchOnMount: 'always',     // Always refetch when component mounts (Lineup tab switch)
  })

  // Clear progress when fetch completes
  useEffect(() => {
    if (!query.isFetching) setPoolProgress(null)
  }, [query.isFetching])

  // ── Injury hash polling — auto-refresh pool on injury changes ──
  // Polls /player-pool/injury-hash every 30s. When the hash changes
  // vs. what we had when the pool was loaded, trigger a refetch.
  // This ensures late scratches (e.g., Bagley OUT) propagate to the
  // dashboard within 30 seconds without manual refresh.
  const lastInjuryHashRef = useRef(null)
  useEffect(() => {
    if (!draftGroupId || !query.data?.players?.length) return

    const interval = setInterval(async () => {
      try {
        const hash = await rotationAPI.getInjuryHash()
        if (!hash) return
        if (lastInjuryHashRef.current && lastInjuryHashRef.current !== hash) {
          devLog(`[Pool] Injury hash changed (${lastInjuryHashRef.current?.slice(0,8)} → ${hash.slice(0,8)}) — auto-refreshing pool`)
          queryClient.invalidateQueries({
            queryKey: ['playerPool', sport, platform, draftGroupId, selectedDate],
          })
        }
        lastInjuryHashRef.current = hash
      } catch { /* silent */ }
    }, 30_000)

    return () => clearInterval(interval)
  }, [draftGroupId, sport, platform, selectedDate, query.data, queryClient])

  // NOTE: preloadPool was removed — it caused a lock deadlock with the
  // SSE stream.  Both called build_player_pool() which acquires the same
  // per-slate build lock.  The preload held the lock for the entire build
  // (without sending progress events), blocking the SSE stream from
  // ever running → "Connecting to NBA API..." for 3+ minutes.
  // The SSE stream alone handles pool building AND caching.

  return {
    pool: query.data?.players ?? EMPTY_PLAYERS,
    excludedPool: query.data?.excludedPlayers ?? EMPTY_EXCLUDED,
    poolLoading: query.isLoading,
    poolFetching: query.isFetching,
    poolProgress,
    poolError: query.error,
    refreshPool: () => {
      // Clear server-side cache then force refetch
      rotationAPI
        .clearPoolCache({ platform, draftGroupId, gameDate: selectedDate, sport })
        .catch(() => {})
      // Use refetchQueries (not just invalidateQueries) to force an immediate
      // re-fetch even if the query is in error/stale state.
      queryClient.refetchQueries({
        queryKey: ['playerPool', sport, platform, draftGroupId, selectedDate],
      })
    },
  }
}
