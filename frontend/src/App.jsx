import React, { useState, useEffect, useCallback, useRef, useMemo, Suspense } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Activity, AlertTriangle, RefreshCw, Calendar, Tv, Users, ListOrdered, MessageSquare, BarChart3, Target } from 'lucide-react'
import { useAppContext } from './context/AppContext'
import { useAuth } from './context/AuthContext'
import useTeams from './hooks/useTeams'
import useScoreboard from './hooks/useScoreboard'
import LoginPage from './components/LoginPage'
import TeamSelector from './components/TeamSelector'
import RotationTable from './components/RotationTable'
import StatsBar from './components/StatsBar'
import InjuryPanel from './components/InjuryPanel'
import GameDayBanner from './components/GameDayBanner'
import SlateView from './components/SlateView'
import DateSelector from './components/DateSelector'
import NewsPanel from './components/NewsPanel'
import ExpertPanel from './components/ExpertPanel'
import ChatPanel from './components/ChatPanel'
import TournamentPanel from './components/TournamentPanel'
import ErrorBoundary from './components/ErrorBoundary'
import LateSwapPanel from './components/LateSwapPanel'
import FadePanel from './components/FadePanel'
import OwnershipSimPanel from './components/OwnershipSimPanel'
import SportSelector from './components/SportSelector'

// Lazy-loaded: heavy view components and modals
const LineupBuilder = React.lazy(() => import('./components/LineupBuilder'))
const AccuracyDashboard = React.lazy(() => import('./components/AccuracyDashboard'))
const UnderdogView = React.lazy(() => import('./components/UnderdogView'))
const GameSimModal = React.lazy(() => import('./components/GameSimModal'))
const PlayerProjectionsModal = React.lazy(() => import('./components/PlayerProjectionsModal'))

const API_BASE = '/api'

function App() {
  const { isAuthenticated, loading: authLoading, user, logout } = useAuth()
  const { view, setView, selectedTeam, setSelectedTeam, selectedDate, setSelectedDate, selectedSport, setSelectedSport, showChat, toggleChat, setShowChat } = useAppContext()

  const queryClient = useQueryClient()

  // ── All hooks MUST be declared before any early returns (Rules of Hooks) ──
  const { data: teams = [], error: teamsError } = useTeams(selectedSport)
  const { data: slateData = null, isLoading: slateLoading } = useScoreboard(selectedDate, selectedSport)

  const [rotation, setRotation] = useState(null)
  const [injuries, setInjuries] = useState([])
  const [gameData, setGameData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  // Modal state
  const [modalTeamId, setModalTeamId] = useState(null)
  const [modalRotation, setModalRotation] = useState(null)
  const [modalLoading, setModalLoading] = useState(false)

  // Lineup sharing (sidebar panels use this)
  const [generatedLineups, setGeneratedLineups] = useState([])
  const handleLineupsGenerated = useCallback((lineups) => {
    setGeneratedLineups(lineups || [])
  }, [])

  // Simulation modal state
  const [simGameId, setSimGameId] = useState(null)
  const [simOverUnder, setSimOverUnder] = useState(null)
  const [simData, setSimData] = useState(null)
  const [simLoading, setSimLoading] = useState(false)
  const [simError, setSimError] = useState(null)

  // Slate player pool state
  const [slatePlayersData, setSlatePlayersData] = useState(null)
  const [slatePlayersLoading, setSlatePlayersLoading] = useState(false)
  const [slatePlayersError, setSlatePlayersError] = useState(null)
  const slatePlayersFetchRef = useRef(0) // guard against stale fetches
  const slateAbortRef = useRef(null) // AbortController for in-flight rotation requests
  const lastSlateArgsRef = useRef(null) // remember args for retry

  // Chat panel state
  const lineupBuilderRef = useRef(null)
  const pendingTimerRef = useRef(null)

  // Cleanup pending chat action timer on unmount
  useEffect(() => {
    return () => {
      if (pendingTimerRef.current) clearTimeout(pendingTimerRef.current)
    }
  }, [])

  // --- AI Chat: action dispatcher ---
  const handleChatAction = useCallback((action) => {
    const builder = lineupBuilderRef.current
    const { type, params } = action

    // Auto-switch to lineup view for actions that need it
    if (!builder && ['lock_player', 'exclude_player', 'unlock_player',
        'generate_lineups', 'analyze_lineups', 'refine_lineups',
        'adjust_projections'].includes(type)) {
      setView('lineup')
      // Queue the action for after LineupBuilder mounts
      pendingTimerRef.current = setTimeout(() => handleChatAction(action), 800)
      return
    }

    if (view !== 'lineup') setView('lineup')
    if (!builder) return

    switch (type) {
      case 'lock_player': {
        const p = builder.resolvePlayerName(params.player_name)
        if (p) builder.lockPlayer(p.player_id)
        break
      }
      case 'unlock_player': {
        const p = builder.resolvePlayerName(params.player_name)
        if (p) builder.unlockPlayer(p.player_id)
        break
      }
      case 'exclude_player': {
        const p = builder.resolvePlayerName(params.player_name)
        if (p) builder.excludePlayer(p.player_id)
        break
      }
      case 'generate_lineups':
        builder.generate({
          numLineups: params.num_lineups,
          strategy: params.strategy,
          contestType: params.contest_type,
        })
        break
      case 'set_strategy':
        builder.setStrategy(params.strategy)
        break
      case 'set_platform':
        builder.setPlatform(params.platform)
        break
      case 'set_contest_type':
        builder.setContestType(params.contest_type)
        break
      case 'analyze_lineups':
        builder.analyze()
        break
      case 'refine_lineups':
        builder.refine()
        break
      case 'adjust_projections': {
        const overrides = {}
        for (const adj of (params.adjustments || [])) {
          const p = builder.resolvePlayerName(adj.player_name)
          if (p) {
            const fields = {}
            if (adj.projected_minutes != null) fields.projected_minutes = adj.projected_minutes
            if (adj.projected_fp != null) fields.projected_fp = adj.projected_fp
            if (adj.floor_fp != null) fields.floor_fp = adj.floor_fp
            if (adj.ceiling_fp != null) fields.ceiling_fp = adj.ceiling_fp
            overrides[p.player_id] = fields
          }
        }
        if (Object.keys(overrides).length > 0) builder.adjustProjections(overrides)
        break
      }
      default:
        console.warn(`[ChatAction] Unknown action type: ${type}`)
    }
  }, [view])

  // --- AI Chat: context getter (called at send-time for freshest state) ---
  const getChatContext = useCallback(() => {
    const builder = lineupBuilderRef.current
    if (!builder) {
      return { platform: 'dk', slate: view === 'slate' ? 'active' : null }
    }
    const ctx = builder.getContext()
    return {
      platform: ctx.platform,
      slate: ctx.activeSlate,
      strategy: ctx.strategy,
      contest_type: ctx.contestType,
      num_lineups: ctx.lineupCount,
      locked_players: ctx.pool
        .filter((p) => ctx.lockedPlayers.includes(p.player_id))
        .map((p) => p.player_name),
      excluded_players: ctx.pool
        .filter((p) => ctx.excludedPlayers.includes(p.player_id))
        .map((p) => p.player_name),
      has_analysis: ctx.hasAnalysis,
      optimizing: ctx.optimizing,
      top_projected: ctx.pool
        .slice()
        .sort((a, b) => b.projected_fp - a.projected_fp)
        .slice(0, 10)
        .map((p) => `${p.player_name} (${p.projected_fp}fp, $${p.salary})`),
      injury_players: ctx.pool
        .filter((p) => p.projected_minutes < 15 && p.salary > 4000)
        .map((p) => `${p.player_name} (${p.projected_minutes}min)`),
      lineup_summary: ctx.lineups.length > 0
        ? ctx.lineups.slice(0, 5).map((lu, i) => ({
            index: i,
            total_salary: lu.total_salary,
            total_fp: lu.total_projected_fp,
            players: lu.players.map((p) => p.player_name).join(', '),
          }))
        : null,
      sport: selectedSport,
    }
  }, [view, selectedSport])

  // Reset slate players + rotation when sport or date changes
  useEffect(() => {
    setSlatePlayersData(null)
    setSlatePlayersError(null)
    setRotation(null)
  }, [selectedSport, selectedDate])

  const fetchRotation = async (teamId, gameDate) => {
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams()
      if (gameDate) params.set('game_date', gameDate)
      if (selectedSport !== 'nba') params.set('sport', selectedSport)
      const qs = params.toString()
      const qsPrefix = qs ? `?${qs}` : ''
      const sportOnly = selectedSport !== 'nba' ? `?sport=${selectedSport}` : ''
      const opts = { credentials: 'include' }
      const [rotRes, injRes, gameRes] = await Promise.all([
        fetch(`${API_BASE}/teams/${teamId}/rotation${qsPrefix}`, opts).then(r => r.json()),
        fetch(`${API_BASE}/teams/${teamId}/injuries${sportOnly}`, opts).then(r => r.json()),
        fetch(`${API_BASE}/teams/${teamId}/game-today${qsPrefix}`, opts)
          .then(r => r.json())
          .catch(() => ({ playing_today: false, game: null })),
      ])
      setRotation(rotRes)
      setInjuries(injRes.injuries || [])
      setGameData(gameRes)
    } catch (err) {
      setError('Failed to fetch rotation data')
      setRotation(null)
      setInjuries([])
      setGameData(null)
    } finally {
      setLoading(false)
    }
  }

  const handleTeamSelect = (team) => {
    setSelectedTeam(team)
    setView('team')
    fetchRotation(team.id, selectedDate)
  }

  const handleRefresh = () => {
    if (view === 'slate' || view === 'lineup') {
      queryClient.invalidateQueries({ queryKey: ['scoreboard'] })
      // Also re-fetch rotation data for the slate player pool
      if (view === 'slate') retrySlateRotations()
    } else if (selectedTeam) {
      fetchRotation(selectedTeam.id, selectedDate)
    }
  }

  const handleDateChange = (newDate) => {
    setSelectedDate(newDate)
    // Teams + slate re-fetch is handled by TanStack Query (query key includes selectedDate).
    // Team view also needs the rotation re-fetched for the new date.
    if (view === 'team' && selectedTeam) {
      fetchRotation(selectedTeam.id, newDate)
    }
  }

  // Modal handlers
  const openTeamModal = async (teamId) => {
    setModalTeamId(teamId)
    setModalRotation(null)
    setModalLoading(true)
    try {
      const params = new URLSearchParams()
      if (selectedDate) params.set('game_date', selectedDate)
      if (selectedSport !== 'nba') params.set('sport', selectedSport)
      const qs = params.toString()
      const res = await fetch(`${API_BASE}/teams/${teamId}/rotation${qs ? `?${qs}` : ''}`, { credentials: 'include' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setModalRotation(await res.json())
    } catch (err) {
      setModalRotation(null)
    } finally {
      setModalLoading(false)
    }
  }

  const closeTeamModal = () => {
    setModalTeamId(null)
    setModalRotation(null)
    setModalLoading(false)
  }

  // Simulation handlers
  const openSimModal = (gameId, overUnder) => {
    setSimGameId(gameId)
    setSimOverUnder(overUnder || null)
    setSimData(null)
    setSimError(null)
    setSimLoading(false)
    // Auto-run immediately
    runSimulation(gameId, overUnder || null)
  }

  const runSimulation = async (gameId, overUnder) => {
    const gId = gameId || simGameId
    const ou = overUnder !== undefined ? overUnder : simOverUnder
    setSimLoading(true)
    setSimError(null)
    try {
      const params = new URLSearchParams({ num_simulations: '10000' })
      if (ou != null) params.set('over_under_line', String(ou))
      if (selectedDate) params.set('game_date', selectedDate)
      if (selectedSport !== 'nba') params.set('sport', selectedSport)
      const res = await fetch(`${API_BASE}/games/${gId}/simulate?${params.toString()}`, { credentials: 'include' })
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.detail || `HTTP ${res.status}`)
      }
      setSimData(await res.json())
    } catch (err) {
      setSimError(err.message || 'Simulation failed. Please try again.')
      setSimData(null)
    } finally {
      setSimLoading(false)
    }
  }

  const closeSimModal = () => {
    setSimGameId(null)
    setSimData(null)
    setSimError(null)
    setSimLoading(false)
    setSimOverUnder(null)
  }

  // ── Player-pool reshape for non-rotation sports (Prompt 7.6) ──────────
  // NBA / CBB use the per-team rotation endpoint (returns minutes-based
  // projections). MLB / NFL don't have rotations — they pull from DK
  // draftables via /player-pool, which returns a flat PlayerPoolEntry
  // list. This helper reshapes that flat list into the same
  // ``[{teamId, teamAbbr, rotation: {projections: [...]}}]`` shape the
  // SlatePlayersPanel already consumes, so the panel renders without
  // sport-specific branching.
  const reshapePoolToRotationShape = (pool) => {
    if (!Array.isArray(pool) || pool.length === 0) return []
    const byTeam = new Map()
    for (const p of pool) {
      const teamAbbr = (p.team_abbreviation || '???').toUpperCase()
      if (!byTeam.has(teamAbbr)) byTeam.set(teamAbbr, [])
      // Map PlayerPoolEntry fields → rotation-projection field names
      // the panel expects. MLB has no projected_minutes (it defaults
      // to 0) — we coerce to 1 for non-injured players so the panel's
      // ``adjusted_minutes > 0`` filter doesn't push every MLB player
      // into the "injured" bucket. Real injury status is preserved
      // via ``play_probability`` and ``reason``.
      const isOut = p.injury_status === 'Out' || p.injury_status === 'Doubtful'
      const minutes = (p.projected_minutes && p.projected_minutes > 0)
        ? p.projected_minutes
        : (isOut ? 0 : 1)
      const playProb = p.injury_status === 'Out' ? 0
        : p.injury_status === 'Doubtful' ? 0.3
        : p.injury_status === 'Questionable' ? 0.7
        : 1.0
      byTeam.get(teamAbbr).push({
        player_id: p.player_id,
        player_name: p.player_name,
        position: p.position,
        // Projection fields (panel reads dk_/fd_-prefixed)
        dk_points: p.projected_fp,
        fd_points: p.projected_fp,
        dk_floor: p.floor_fp,
        fd_floor: p.floor_fp,
        dk_ceiling: p.ceiling_fp,
        fd_ceiling: p.ceiling_fp,
        dk_salary: p.salary,
        fd_salary: p.salary,
        // dk_value (FP per $1K) is computed by the backend's pool
        // builder; pass it through if present, else null and the
        // panel renders an em-dash.
        dk_value: p.dk_value ?? null,
        adjusted_minutes: minutes,
        play_probability: playProb,
        reason: p.injury_description || p.injury_status || '',
        // projected_stats is shown in the per-stat columns; preserve
        // when present (CSV imports may carry it for MLB hitters).
        projected_stats: p.projected_stats || {},
        // Pass through the env-multiplier fields so the panel can
        // surface MLB park/wind / NFL wind adjustments alongside the
        // raw projections (consumed by EnvMultiplierBadge in the
        // builder; the slate panel doesn't render them today but
        // having them on the wire keeps a future enhancement cheap).
        adjusted_fp: p.adjusted_fp,
        env_multiplier: p.env_multiplier,
      })
    }
    // Stable id per team (numeric for back-compat with anything that
    // expects a number — using a hash-by-position is fine since the
    // panel only displays teamAbbr).
    let idCounter = 1
    const result = []
    for (const [teamAbbr, projections] of byTeam) {
      result.push({
        teamId: idCounter++,
        teamAbbr,
        rotation: { projections },
      })
    }
    return result
  }

  // ── Universal slate player pool fetch (Prompt 7.7) ───────────────────
  // The slate-page Player Pool panel used to fan out to /teams/{id}/rotation
  // for every team in the slate (NBA's per-team rotation engine). That
  // model only exists for NBA + CBB — MLB and NFL silently 30x failed
  // and surfaced the misleading "NBA API may be slow" copy.
  //
  // The replacement is a single sport-agnostic call to /api/player-pool
  // — the same endpoint that powers the LineupBuilder's player table.
  // It returns a flat list of PlayerPoolEntry records with salaries,
  // projections, and (when available) per-sport enrichment like minutes
  // for NBA. We reshape the flat list into the same {teamId, teamAbbr,
  // rotation: {projections}} shape SlatePlayersPanel + ValuePlaysPanel +
  // PropsComparisonPanel already consume, so the downstream contract
  // is preserved while the fetch is universalized.
  const fetchSlateRotations = useCallback(async (games, draftGroupId, gameDate) => {
    if (!games || games.length === 0) {
      setSlatePlayersData(null)
      setSlatePlayersError(null)
      return
    }

    // Save args so we can retry later
    lastSlateArgsRef.current = { games, draftGroupId, gameDate }

    // Without a draft_group_id we can't query DK draftables — surface a
    // clear, sport-aware message instead of hitting the endpoint and
    // 422-ing. The most common case for MLB/NFL is that DK hasn't
    // published Classic contests for this date yet (or the backend
    // sticky cache is cold after a restart) — different problem from
    // a transient network issue, so the copy distinguishes.
    if (!draftGroupId) {
      const sportLabel = selectedSport.toUpperCase()
      const isProjectionSport = (
        selectedSport === 'mlb' || selectedSport === 'nfl'
      )
      setSlatePlayersData(null)
      setSlatePlayersError(
        isProjectionSport
          ? `${sportLabel} contests not yet published in the DK lobby ` +
            `for this date. DK typically posts Classic contests 6–12 ` +
            `hours before first pitch — try Refresh closer to start ` +
            `time, or check back later.`
          : `No DraftKings draft group resolved for this slate yet — ` +
            `check back closer to lineup lock.`
      )
      setSlatePlayersLoading(false)
      return
    }

    // Abort any in-flight request from a previous batch
    if (slateAbortRef.current) {
      slateAbortRef.current.abort()
    }
    const controller = new AbortController()
    slateAbortRef.current = controller

    const fetchId = ++slatePlayersFetchRef.current
    setSlatePlayersLoading(true)
    setSlatePlayersData(null)
    setSlatePlayersError(null)

    try {
      const qp = new URLSearchParams()
      qp.set('platform', 'dk')
      qp.set('draft_group_id', String(draftGroupId))
      if (gameDate) qp.set('game_date', gameDate)
      qp.set('sport', selectedSport)

      const res = await fetch(
        `${API_BASE}/player-pool?${qp.toString()}`,
        { credentials: 'include', signal: controller.signal }
      )
      if (!res.ok) {
        let detail = `HTTP ${res.status}`
        try {
          const blob = await res.json()
          if (blob?.detail) detail = blob.detail
        } catch { /* swallow JSON parse */ }
        throw new Error(detail)
      }

      const data = await res.json()
      const players = data?.players || []
      const reshaped = reshapePoolToRotationShape(players)

      if (fetchId !== slatePlayersFetchRef.current) return

      if (reshaped.length === 0) {
        setSlatePlayersData(null)
        // Generic, sport-aware empty state. NBA / CBB historically saw
        // an "NBA API unavailable" message — that's gone.
        const isProjectionSport = selectedSport === 'mlb' || selectedSport === 'nfl'
        setSlatePlayersError(
          isProjectionSport
            ? `No ${selectedSport.toUpperCase()} players returned for this slate. ` +
              `Open the Lineup tab and use Import Proj to upload a projections CSV.`
            : `No ${selectedSport.toUpperCase()} players returned for this slate. ` +
              `The pool builder may still be warming up — try Retry.`
        )
      } else {
        setSlatePlayersData(reshaped)
        setSlatePlayersError(null)
      }
    } catch (err) {
      if (err.name === 'AbortError') return
      if (fetchId === slatePlayersFetchRef.current) {
        setSlatePlayersData(null)
        setSlatePlayersError(
          `Failed to load player pool for this slate: ${err.message}.`
        )
      }
    } finally {
      if (fetchId === slatePlayersFetchRef.current) {
        setSlatePlayersLoading(false)
      }
    }
  }, [selectedSport])

  const handleSlateChange = useCallback(
    (games, draftGroupId) => {
      fetchSlateRotations(games, draftGroupId, selectedDate)
    },
    [fetchSlateRotations, selectedDate]
  )

  const retrySlateRotations = useCallback(() => {
    const args = lastSlateArgsRef.current
    if (args) {
      fetchSlateRotations(args.games, args.draftGroupId, args.gameDate)
    }
  }, [fetchSlateRotations])

  const isRefreshing = (view === 'slate' || view === 'lineup' || view === 'accuracy') ? slateLoading : loading

  // Memoize computed props to prevent unnecessary child re-renders
  const slateTeamIds = useMemo(() => {
    if (view === 'slate' && slateData && slateData.games) {
      return [...new Set(
        slateData.games.flatMap((g) => [g.home_team.team_id, g.away_team.team_id])
      )]
    }
    return null
  }, [view, slateData])

  const expertPlayerNames = useMemo(() => {
    if (view === 'team' && rotation) {
      return rotation.projections.map((p) => p.player_name)
    }
    return null
  }, [view, rotation])

  // ── Auth Guard (after all hooks) ─────────────────────────────────────
  if (authLoading) {
    return (
      <div className="min-h-screen bg-ticker-bg flex items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <Activity className="w-8 h-8 text-ticker-green animate-pulse" />
          <p className="text-sm text-ticker-muted">Loading...</p>
        </div>
      </div>
    )
  }

  if (!isAuthenticated) {
    return <LoginPage />
  }

  return (
    <div className="min-h-screen bg-ticker-bg">
      {/* Header */}
      <header className="border-b border-ticker-border bg-ticker-card/50 backdrop-blur-sm sticky top-0 z-10">
        <div className="max-w-[1920px] mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Activity className="w-6 h-6 text-ticker-green" />
            <h1 className="text-xl font-bold tracking-tight">
              <span className="text-ticker-green">Rotation</span>
              <span className="text-white">Engine</span>
            </h1>
            <span className="text-xs text-ticker-muted border border-ticker-border px-2 py-0.5 rounded">
              v1.0
            </span>
            <SportSelector selectedSport={selectedSport} onSelect={setSelectedSport} />
          </div>

          <div className="flex items-center gap-4">
            {/* Tab Toggle */}
            <div className="flex items-center bg-ticker-bg rounded-md p-0.5 border border-ticker-border" role="navigation" aria-label="Main navigation">
              <button
                onClick={() => setView('slate')}
                aria-pressed={view === 'slate'}
                aria-label="Switch to slate view"
                className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded transition-colors ${
                  view === 'slate'
                    ? 'bg-ticker-green text-white'
                    : 'text-ticker-muted hover:text-white'
                }`}
              >
                <Tv className="w-3.5 h-3.5" />
                Slate
              </button>
              <button
                onClick={() => setView('team')}
                aria-pressed={view === 'team'}
                aria-label="Switch to team view"
                className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded transition-colors ${
                  view === 'team'
                    ? 'bg-ticker-green text-white'
                    : 'text-ticker-muted hover:text-white'
                }`}
              >
                <Users className="w-3.5 h-3.5" />
                Team
              </button>
              <button
                onClick={() => setView('lineup')}
                aria-pressed={view === 'lineup'}
                aria-label="Switch to lineup view"
                className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded transition-colors ${
                  view === 'lineup'
                    ? 'bg-ticker-green text-white'
                    : 'text-ticker-muted hover:text-white'
                }`}
              >
                <ListOrdered className="w-3.5 h-3.5" />
                Lineup
              </button>
              <button
                onClick={() => setView('accuracy')}
                aria-pressed={view === 'accuracy'}
                aria-label="Switch to accuracy view"
                className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded transition-colors ${
                  view === 'accuracy'
                    ? 'bg-ticker-green text-white'
                    : 'text-ticker-muted hover:text-white'
                }`}
              >
                <BarChart3 className="w-3.5 h-3.5" />
                Accuracy
              </button>
              <button
                onClick={() => setView('underdog')}
                aria-pressed={view === 'underdog'}
                aria-label="Switch to Underdog pick'em view"
                className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded transition-colors ${
                  view === 'underdog'
                    ? 'bg-ticker-green text-white'
                    : 'text-ticker-muted hover:text-white'
                }`}
              >
                <Target className="w-3.5 h-3.5" />
                Underdog
              </button>
            </div>

            <button
              onClick={toggleChat}
              aria-label="Toggle AI Chat panel"
              className={`flex items-center gap-2 px-3 py-1.5 text-sm border rounded transition-colors ${
                showChat
                  ? 'border-ticker-green bg-ticker-green/10 text-ticker-green'
                  : 'border-ticker-border text-ticker-muted hover:bg-ticker-card hover:text-white'
              }`}
            >
              <MessageSquare className="w-4 h-4" />
              AI Chat
            </button>

            <button
              onClick={handleRefresh}
              disabled={isRefreshing}
              aria-label="Refresh data"
              className="flex items-center gap-2 px-3 py-1.5 text-sm border border-ticker-border
                       rounded hover:bg-ticker-card transition-colors disabled:opacity-50"
            >
              <RefreshCw className={`w-4 h-4 ${isRefreshing ? 'animate-spin' : ''}`} />
              Refresh
            </button>

            {/* User + Logout */}
            {user && user.provider !== 'none' && (
              <div className="flex items-center gap-2 border-l border-ticker-border pl-4 ml-2">
                {user.avatar_url && (
                  <img src={user.avatar_url} alt="" className="w-7 h-7 rounded-full" />
                )}
                <span className="text-xs text-ticker-muted hidden sm:inline">
                  {user.display_name || user.email}
                </span>
                <button
                  onClick={logout}
                  className="text-xs text-ticker-muted hover:text-white transition-colors"
                >
                  Sign out
                </button>
              </div>
            )}
          </div>
        </div>
      </header>

      {/* Date Selector Bar */}
      <DateSelector
        selectedDate={selectedDate}
        onDateChange={handleDateChange}
      />

      {/* Main Content — three-column layout: expert | content | news */}
      <div className="max-w-[1920px] mx-auto px-4 py-6 flex flex-col 2xl:flex-row gap-4">
        {/* Left: Expert Signals or AI Chat Panel (collapses below 2xl) */}
        <aside className="w-full 2xl:w-[350px] 2xl:flex-shrink-0 order-3 2xl:order-1">
          <div className="2xl:sticky 2xl:top-[60px] space-y-4">
            {showChat ? (
              <ErrorBoundary>
                <ChatPanel
                  context={getChatContext}
                  onAction={handleChatAction}
                />
              </ErrorBoundary>
            ) : null}
            <ErrorBoundary>
              <ExpertPanel
                teamAbbr={
                  view === 'team' && selectedTeam
                    ? selectedTeam.abbreviation
                    : null
                }
                playerNames={expertPlayerNames}
                sport={selectedSport}
              />
            </ErrorBoundary>
            <ErrorBoundary>
              <TournamentPanel sport={selectedSport} />
            </ErrorBoundary>
            {view === 'lineup' && (
              <ErrorBoundary>
                <LateSwapPanel gameDate={selectedDate} lineups={generatedLineups} slateData={slateData} sport={selectedSport} />
              </ErrorBoundary>
            )}
            {view === 'lineup' && (
              <ErrorBoundary>
                <FadePanel sport={selectedSport} />
              </ErrorBoundary>
            )}
            {view === 'lineup' && (
              <ErrorBoundary>
                <OwnershipSimPanel lineup={generatedLineups.length > 0 ? generatedLineups[0] : null} sport={selectedSport} />
              </ErrorBoundary>
            )}
          </div>
        </aside>

        {/* Center: primary content area */}
        <main className="flex-1 min-w-0 order-1 2xl:order-2" role="main">
          {/* ==================== SLATE VIEW ==================== */}
          {view === 'slate' && (
            <ErrorBoundary>
              <SlateView
                slate={slateData}
                loading={slateLoading}
                onOpenTeamModal={openTeamModal}
                onSimulate={openSimModal}
                playersData={slatePlayersData}
                playersLoading={slatePlayersLoading}
                playersError={slatePlayersError}
                onRetryPlayers={retrySlateRotations}
                onSlateChange={handleSlateChange}
                selectedDate={selectedDate}
                sport={selectedSport}
              />
            </ErrorBoundary>
          )}

          {/* ==================== LINEUP VIEW ==================== */}
          {view === 'lineup' && (
            <ErrorBoundary>
              <Suspense fallback={<div className="flex items-center justify-center h-64 text-gray-400">Loading lineup builder...</div>}>
                <LineupBuilder
                  ref={lineupBuilderRef}
                  slateData={slateData}
                  selectedDate={selectedDate}
                  onLineupsGenerated={handleLineupsGenerated}
                  sport={selectedSport}
                />
              </Suspense>
            </ErrorBoundary>
          )}

          {/* ==================== ACCURACY VIEW ==================== */}
          {view === 'accuracy' && (
            <ErrorBoundary>
              <Suspense fallback={<div className="flex items-center justify-center h-64 text-gray-400">Loading accuracy dashboard...</div>}>
                <AccuracyDashboard sport={selectedSport} />
              </Suspense>
            </ErrorBoundary>
          )}

          {/* ==================== UNDERDOG VIEW ==================== */}
          {view === 'underdog' && (
            <ErrorBoundary>
              <Suspense fallback={<div className="flex items-center justify-center h-64 text-gray-400">Loading underdog view...</div>}>
                <UnderdogView selectedDate={selectedDate} sport={selectedSport} />
              </Suspense>
            </ErrorBoundary>
          )}

          {/* ==================== TEAM VIEW ==================== */}
          {view === 'team' && (
            <ErrorBoundary>
              {/* Team Selector */}
              <TeamSelector
                teams={teams}
                selectedTeam={selectedTeam}
                onSelect={handleTeamSelect}
                sport={selectedSport}
              />

              {/* Game Day Banner - shows when team has a game on selected date */}
              {gameData && gameData.playing_today && !loading && (
                <GameDayBanner gameData={gameData} selectedTeam={selectedTeam} selectedDate={selectedDate} />
              )}

              {/* No Game notice */}
              {gameData && !gameData.playing_today && !loading && selectedTeam && (
                <div className="mt-4 flex items-center gap-3 p-3 bg-ticker-card border border-ticker-border rounded-lg">
                  <Calendar className="w-5 h-5 text-ticker-muted flex-shrink-0" />
                  <span className="text-sm text-ticker-muted">
                    {selectedTeam.full_name} do not play on this date.
                  </span>
                </div>
              )}

              {/* Error Banner */}
              {(error || teamsError) && (
                <div className="mt-4 flex items-center gap-3 p-3 bg-red-900/20 border border-red-800/50 rounded-lg">
                  <AlertTriangle className="w-5 h-5 text-ticker-red flex-shrink-0" />
                  <span className="text-sm text-red-300">
                    {error || 'Failed to load teams. Is the backend running?'}
                  </span>
                </div>
              )}

              {/* Loading State */}
              {loading && (
                <div className="mt-8 flex flex-col items-center gap-3">
                  <RefreshCw className="w-8 h-8 text-ticker-green animate-spin" />
                  <p className="text-sm text-ticker-muted">Projecting rotation...</p>
                </div>
              )}

              {/* Results */}
              {rotation && !loading && (
                <div className="mt-6 space-y-6">
                  {/* Stats Summary Bar */}
                  <StatsBar rotation={rotation} />

                  {/* Two Column Layout */}
                  <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                    {/* Rotation Table (2/3) */}
                    <div className="lg:col-span-2">
                      <RotationTable rotation={rotation} />
                    </div>

                    {/* Injury Panel (1/3) */}
                    <div>
                      <InjuryPanel injuries={injuries} />
                    </div>
                  </div>
                </div>
              )}

              {/* Empty State */}
              {!rotation && !loading && !error && (
                <div className="mt-16 text-center">
                  <Activity className="w-16 h-16 text-ticker-border mx-auto mb-4" />
                  <h2 className="text-lg font-semibold text-gray-400 mb-2">
                    Select a Team
                  </h2>
                  <p className="text-sm text-ticker-muted max-w-md mx-auto">
                    Choose an NBA team above to generate real-time minutes projections
                    using the Waterfall Method and coach-specific adjustments.
                  </p>
                </div>
              )}
            </ErrorBoundary>
          )}
        </main>

        {/* Right: News Panel (fixed-width, collapses below 2xl) */}
        <aside className="w-full 2xl:w-[340px] 2xl:flex-shrink-0 order-2 2xl:order-3">
          <div className="2xl:sticky 2xl:top-[60px]">
            <ErrorBoundary>
              <NewsPanel
                teamIds={view === 'team' && selectedTeam ? [selectedTeam.id] : null}
                slateTeamIds={slateTeamIds
                }
                sport={selectedSport}
              />
            </ErrorBoundary>
          </div>
        </aside>
      </div>

      {/* Player Projections Modal */}
      {modalTeamId != null && (
        <Suspense fallback={null}>
          <PlayerProjectionsModal
            teamId={modalTeamId}
            rotation={modalRotation}
            loading={modalLoading}
            onClose={closeTeamModal}
          />
        </Suspense>
      )}

      {/* Game Simulation Modal */}
      {simGameId != null && (
        <Suspense fallback={null}>
          <GameSimModal
            gameId={simGameId}
            simData={simData}
            loading={simLoading}
            error={simError}
            onClose={closeSimModal}
            onRunSim={runSimulation}
          />
        </Suspense>
      )}

      {/* Footer */}
      <footer className="border-t border-ticker-border mt-auto py-4">
        <div className="max-w-[1920px] mx-auto px-4 flex items-center justify-between text-xs text-ticker-muted">
          <span>RotationEngine v1.0 | Data: {selectedSport === 'cbb' ? 'espn / cbbpy' : 'nba_api'}</span>
          <span>240-Minute Constraint Enforced</span>
        </div>
      </footer>
    </div>
  )
}

export default App
