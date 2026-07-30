/**
 * useLineupState — Custom hook that owns all state, effects, and handlers
 * for the LineupBuilder component.
 *
 * Extracts ~700 lines of useState/useEffect/useCallback/useMemo logic
 * so LineupBuilder.jsx is a thin UI orchestrator.
 */

import { useState, useEffect, useCallback, useRef, useMemo, useImperativeHandle } from 'react'
import { rotationAPI } from '../services/api'
import { getLocalToday } from '../utils/dateUtils'
import usePlayerPool from './usePlayerPool'

export default function useLineupState({ slateData, selectedDate, onLineupsGenerated, sport = 'nba', ref }) {
  // ── Platform & slate ─────────────────────────────────────────────
  const [platform, setPlatform] = useState('dk')
  const [activeSlate, setActiveSlate] = useState(null)

  // ── Derived slate (must precede usePlayerPool) ──────────────────
  const hasSlates = slateData && slateData.slates && slateData.slates.length > 0
  const slates = hasSlates ? slateData.slates : []
  const currentSlateName = activeSlate || (slates.length > 0 ? slates[0].name : null)
  const currentSlate = slates.find((s) => s.name === currentSlateName)
  const draftGroupId = currentSlate?.draft_group_id
  const isLateSwap = currentSlate?.late_swap_active || false

  // ── Player pool (via TanStack Query) ────────────────────────────
  const {
    pool: originalPool,
    excludedPool,
    poolLoading,
    poolFetching,
    poolProgress,
    poolError,
    refreshPool: handleRefreshPool,
  } = usePlayerPool({ sport, platform, draftGroupId, selectedDate })

  const [projectionOverrides, setProjectionOverrides] = useState({})

  // ── Multi-lineup state ───────────────────────────────────────────
  const [lineups, setLineups] = useState([])
  const [baselineScore, setBaselineScore] = useState(null)
  // Full optimal lineup (players + salary) used to compute baselineScore — so
  // the UI can show *which* players form the theoretical max, not just FP.
  const [baselineLineup, setBaselineLineup] = useState(null)
  const [lineupDraftGroupId, setLineupDraftGroupId] = useState(null)
  const [selectedLineupIdx, setSelectedLineupIdx] = useState(0)
  const [numLineups, setNumLineups] = useState(1)
  const [strategy, setStrategy] = useState('max_projection')
  const [optimizing, setOptimizing] = useState(false)
  const [optimizeError, setOptimizeError] = useState(null)

  // Contest type
  const [contestType, setContestType] = useState('gpp')

  // Showdown mode
  const [mode, setMode] = useState('classic')
  const [showdownGameId, setShowdownGameId] = useState(null)

  // Exposure limit
  const [maxExposure, setMaxExposure] = useState(null)

  // Recent form weight override (null = system default 0.25)
  const [recentWeight, setRecentWeight] = useState(null)

  // Optimality floor threshold (0.75-1.0, default 0.90 = 90%)
  const [optimalityThreshold, setOptimalityThreshold] = useState(0.90)

  // Deterministic mode (seed-based reproducibility)
  const [deterministicMode, setDeterministicMode] = useState(false)

  // Game stacking toggle (true = enabled)
  const [enableStacking, setEnableStacking] = useState(true)

  // ── Dynamic stacking overrides (Prompt 5.3) ────────────────────────
  // null = "use sport default" — the backend's per-sport stack_rules
  // kick in when the API receives no override. Setting any of these
  // sends the corresponding field on /generate-lineups so the
  // optimizer's helper picks up the user's value.
  //   NFL → primaryStackSize = qb_min_pass_catchers
  //         requireBringBack = require_bring_back
  //   MLB → primaryStackSize = primary_stack_size
  //         secondaryStackSize = secondary_stack_size
  const [primaryStackSize, setPrimaryStackSize] = useState(null)
  const [secondaryStackSize, setSecondaryStackSize] = useState(null)
  const [requireBringBack, setRequireBringBack] = useState(null)

  // Reset stacking overrides when the sport changes — the dropdowns are
  // sport-specific (NFL: primary ∈ [1,2,3]; MLB: primary ∈ [5,4,3]) so
  // a stale value from a previous sport would either render as a missing
  // option (UI confusion) or trip the backend model_validator (e.g. MLB
  // 4-stack carrying into NFL where primary=4 isn't a valid QB-stack).
  useEffect(() => {
    setPrimaryStackSize(null)
    setSecondaryStackSize(null)
    setRequireBringBack(null)
  }, [sport])

  // Salary floor percentage (0.90-1.00, default 0.98)
  const [salaryFloorPct, setSalaryFloorPct] = useState(0.95)

  // Per-player exposure overrides { playerId: maxExposure (0-1) }
  const [playerExposures, setPlayerExposures] = useState({})

  // ── Analysis ─────────────────────────────────────────────────────
  const [analysis, setAnalysis] = useState(null)
  const [analyzing, setAnalyzing] = useState(false)

  // ── Refinement ───────────────────────────────────────────────────
  const [refining, setRefining] = useState(false)
  const [refineSteps, setRefineSteps] = useState([])
  const [lastRefineResult, setLastRefineResult] = useState(null)

  // ── Lock / exclude ───────────────────────────────────────────────
  const [lockedPlayers, setLockedPlayers] = useState(new Set())
  const [excludedPlayers, setExcludedPlayers] = useState(new Set())

  // ── DK Upload Modal ──────────────────────────────────────────────
  const [dkUploadOpen, setDkUploadOpen] = useState(false)

  // ── DK Exporter Modal ─────────────────────────────────────────
  const [dkExporterOpen, setDkExporterOpen] = useState(false)

  // ── Grid sort state (shared with LineupGrid for export) ────────
  // { key: 'grade'|'proj'|'floor'|'ceil'|'salary', dir: 'asc'|'desc' } or null
  const [gridSortConfig, setGridSortConfig] = useState(null)

  // ── Export feedback ──────────────────────────────────────────────
  const [copied, setCopied] = useState(false)
  const autoAnalyzeTimerRef = useRef(null)
  const copiedTimerRef = useRef(null)

  // ── Progress tracking ────────────────────────────────────────────
  const [optimizeSteps, setOptimizeSteps] = useState([])
  const [analyzeSteps, setAnalyzeSteps] = useState([])

  // ── Effects ──────────────────────────────────────────────────────

  // Notify parent when lineups change (for OwnershipSimPanel, LateSwapPanel).
  // Skip the initial mount (lineups=[]) to prevent an infinite re-render loop:
  // mount → effect fires with [] → parent setState([]) → new ref → re-render → loop.
  const lineupsInitRef = useRef(true)
  useEffect(() => {
    if (lineupsInitRef.current) {
      lineupsInitRef.current = false
      return
    }
    if (onLineupsGenerated) {
      onLineupsGenerated(lineups)
    }
  }, [lineups, onLineupsGenerated])

  // Cleanup pending timers on unmount
  useEffect(() => {
    return () => {
      if (autoAnalyzeTimerRef.current) clearTimeout(autoAnalyzeTimerRef.current)
      if (copiedTimerRef.current) clearTimeout(copiedTimerRef.current)
    }
  }, [])

  // ── Derived state ────────────────────────────────────────────────
  const isDK = platform === 'dk'

  // Detect slate mismatch: lineups were generated for a different draft group
  const slateMismatch = lineupDraftGroupId && draftGroupId && lineupDraftGroupId !== draftGroupId
  const lineupSlateName = slateMismatch
    ? slates.find((s) => s.draft_group_id === lineupDraftGroupId)?.name || `DG ${lineupDraftGroupId}`
    : null

  // Available games for showdown picker
  const availableGames = useMemo(() => {
    if (!originalPool || originalPool.length === 0) return []
    const gameMap = {}
    for (const p of originalPool) {
      if (p.game_id && p.team_abbreviation) {
        if (!gameMap[p.game_id]) {
          gameMap[p.game_id] = new Set()
        }
        gameMap[p.game_id].add(p.team_abbreviation)
      }
    }
    return Object.entries(gameMap)
      .filter(([, teams]) => teams.size >= 2)
      .map(([game_id, teams]) => {
        const sorted = [...teams].sort()
        return { game_id, label: sorted.join(' vs '), teams: sorted }
      })
      .sort((a, b) => a.label.localeCompare(b.label))
  }, [originalPool])

  // Display pool: original + user overrides
  const displayPool = useMemo(() => {
    if (Object.keys(projectionOverrides).length === 0) return originalPool
    return originalPool.map((player) => {
      const overrides = projectionOverrides[player.player_id]
      if (!overrides) return player
      const updated = { ...player, ...overrides }
      if (updated.salary > 0) {
        updated.dk_value = Math.round((updated.projected_fp / updated.salary) * 1000 * 100) / 100
      }
      return updated
    })
  }, [originalPool, projectionOverrides])

  const hasOverrides = Object.keys(projectionOverrides).length > 0

  // Reset overrides when pool data changes (new slate / date / platform)
  useEffect(() => {
    setProjectionOverrides({})
  }, [originalPool])

  // Reset lineups + locks on slate change
  useEffect(() => {
    setLockedPlayers(new Set())
    setExcludedPlayers(new Set())
    setLineups([])
    setBaselineScore(null)
    setBaselineLineup(null)
    setAnalysis(null)
  }, [draftGroupId])

  // ── Generate ─────────────────────────────────────────────────────
  const handleGenerate = async (overrides = {}) => {
    if (!draftGroupId) return

    const effectiveNumLineups = overrides.numLineups ?? numLineups
    const effectiveStrategy = overrides.strategy ?? strategy
    const effectiveContestType = overrides.contestType ?? contestType

    if (overrides.numLineups) setNumLineups(overrides.numLineups)
    if (overrides.strategy) setStrategy(overrides.strategy)
    if (overrides.contestType) setContestType(overrides.contestType)

    setOptimizing(true)
    setOptimizeError(null)
    setAnalysis(null)

    const isSimFilter = effectiveStrategy === 'sim_filter'
    const isMulti = effectiveNumLineups > 1
    const steps = isSimFilter
      ? [
          { label: 'Building pool', done: false },
          { label: 'Running 1000 simulations', done: false },
          { label: 'Solving iterations', done: false },
          { label: 'Ranking by frequency', done: false },
        ]
      : isMulti
      ? [
          { label: 'Building pool', done: false },
          { label: 'Enriching data', done: false },
          { label: 'Simulating games', done: false },
          { label: `Generating ${effectiveNumLineups} lineups`, done: false },
          { label: 'Enforcing diversity', done: false },
        ]
      : [
          { label: 'Building pool', done: false },
          { label: 'Optimizing lineup', done: false },
          { label: 'Improving swaps', done: false },
        ]
    setOptimizeSteps(steps.map((s) => ({ ...s })))

    const stepTimers = []
    const estimatedMs = isMulti ? effectiveNumLineups * 3000 + 5000 : 5000
    const perStep = estimatedMs / steps.length
    steps.forEach((_, i) => {
      if (i === 0) return
      stepTimers.push(
        setTimeout(() => {
          setOptimizeSteps((prev) =>
            prev.map((s, j) => (j < i ? { ...s, done: true } : s))
          )
        }, perStep * i)
      )
    })

    try {
      if (effectiveStrategy === 'sim_filter') {
        // ── Simulate & Filter pipeline (A/B alternative) ────────
        const result = await rotationAPI.simFilterLineups({
          platform,
          draftGroupId,
          gameDate: selectedDate,
          lockedPlayers: [...lockedPlayers],
          excludedPlayers: [...excludedPlayers],
          numSimulations: 1000,
          numLineups: effectiveNumLineups,
          solverMode: 'greedy',
          contestType: effectiveContestType,
          projectionOverrides: hasOverrides ? projectionOverrides : null,
          mode,
          gameId: mode === 'showdown' ? showdownGameId : null,
          sport,
        })
        setLineups((result.lineups || []).map((sl) => sl.lineup))
        setBaselineScore(null)
        setBaselineLineup(null)
      } else if (effectiveNumLineups === 1) {
        const result = await rotationAPI.optimizeLineup({
          platform,
          draftGroupId,
          gameDate: selectedDate,
          lockedPlayers: [...lockedPlayers],
          excludedPlayers: [...excludedPlayers],
          projectionOverrides: hasOverrides ? projectionOverrides : null,
          mode,
          gameId: mode === 'showdown' ? showdownGameId : null,
          sport,
          contestType: effectiveContestType,
          seed: deterministicMode ? 42 : undefined,
        })
        setLineups([result])
        setBaselineScore(null)
        setBaselineLineup(null)
      } else {
        const result = await rotationAPI.generateLineups({
          platform,
          draftGroupId,
          gameDate: selectedDate,
          lockedPlayers: [...lockedPlayers],
          excludedPlayers: [...excludedPlayers],
          numLineups: effectiveNumLineups,
          strategy: effectiveStrategy,
          contestType: effectiveContestType,
          projectionOverrides: hasOverrides ? projectionOverrides : null,
          mode,
          gameId: mode === 'showdown' ? showdownGameId : null,
          maxExposure,
          recentWeight,
          sport,
          optimalityThreshold,
          isLateSwap,
          seed: deterministicMode ? 42 : undefined,
          enableStacking,
          salaryFloorPct,
          playerMaxExposure: Object.keys(playerExposures).length > 0 ? playerExposures : undefined,
          primaryStackSize,
          secondaryStackSize,
          requireBringBack,
        })
        const lineups = result.lineups || []
        if (lineups.length === 0) {
          const warns = (result.warnings || []).join('; ')
          throw new Error(
            warns || `No viable lineups generated (${result.num_candidates_generated || 0} candidates tried). Try adjusting locked/excluded players.`
          )
        }
        setLineups(lineups)
        setBaselineScore(result.baseline_projection_score ?? null)
        setBaselineLineup(result.baseline_optimal_lineup ?? null)
      }
      setSelectedLineupIdx(0)
      setLineupDraftGroupId(draftGroupId)
      setOptimizeSteps((prev) => prev.map((s) => ({ ...s, done: true })))
    } catch (err) {
      console.error('Optimization failed:', err)
      const msg = err?.message || String(err)
      if (msg.includes('504') || msg.includes('timeout') || msg.includes('time limit')) {
        setOptimizeError(
          `Generation timed out for ${effectiveNumLineups} lineups. Try fewer lineups or a smaller slate.`
        )
      } else {
        setOptimizeError(`Lineup generation failed: ${msg}`)
      }
    } finally {
      stepTimers.forEach(clearTimeout)
      setOptimizing(false)
      setOptimizeSteps([])
    }
  }

  // ── Analyze ──────────────────────────────────────────────────────
  const handleAnalyze = async () => {
    if (!lineups.length || !draftGroupId) return
    setAnalyzing(true)

    const steps = [
      { label: 'Scoring dimensions', done: false },
      { label: 'Identifying risks', done: false },
      { label: 'Finding swap targets', done: false },
      { label: 'Grading lineups', done: false },
    ]
    setAnalyzeSteps(steps.map((s) => ({ ...s })))

    const stepTimers = []
    const estimatedMs = lineups.length * 1500 + 2000
    const perStep = estimatedMs / steps.length
    steps.forEach((_, i) => {
      if (i === 0) return
      stepTimers.push(
        setTimeout(() => {
          setAnalyzeSteps((prev) =>
            prev.map((s, j) => (j < i ? { ...s, done: true } : s))
          )
        }, perStep * i)
      )
    })

    try {
      const result = await rotationAPI.analyzeLineups({
        platform,
        draftGroupId,
        gameDate: selectedDate,
        lineups,
        sport,
      })
      setAnalysis(result)
      setAnalyzeSteps((prev) => prev.map((s) => ({ ...s, done: true })))
    } catch (err) {
      console.error('Analysis failed:', err)
    } finally {
      stepTimers.forEach(clearTimeout)
      setAnalyzing(false)
      setAnalyzeSteps([])
    }
  }

  // ── Refine ───────────────────────────────────────────────────────
  const handleRefine = async () => {
    if (!lineups.length || !draftGroupId) return
    setRefining(true)
    setLastRefineResult(null)

    const steps = [
      { label: 'Analyzing lineups', done: false },
      { label: 'Evaluating swaps', done: false },
      { label: 'Applying improvements', done: false },
      { label: 'Re-scoring lineups', done: false },
    ]
    setRefineSteps(steps.map((s) => ({ ...s })))

    const stepTimers = []
    const estimatedMs = lineups.length * 2000 + 4000
    const perStep = estimatedMs / steps.length
    steps.forEach((_, i) => {
      if (i === 0) return
      stepTimers.push(
        setTimeout(() => {
          setRefineSteps((prev) =>
            prev.map((s, j) => (j < i ? { ...s, done: true } : s))
          )
        }, perStep * i)
      )
    })

    try {
      const result = await rotationAPI.refineLineups({
        platform,
        draftGroupId,
        gameDate: selectedDate,
        lineups,
        maxIterations: 3,
        targetGrade: 'A',
        sport,
      })

      if (result.lineups && result.lineups.length > 0) {
        setLineups(result.lineups)
      }
      setLastRefineResult(result)
      setAnalysis(null)
      setRefineSteps((prev) => prev.map((s) => ({ ...s, done: true })))

      // Auto-analyze after refinement
      autoAnalyzeTimerRef.current = setTimeout(() => {
        handleAnalyze()
      }, 300)
    } catch (err) {
      console.error('Refinement failed:', err)
    } finally {
      stepTimers.forEach(clearTimeout)
      setRefining(false)
      setRefineSteps([])
    }
  }

  // ── Sorted lineups (respects grid sort for export) ──────────────
  // Mirrors the sorting logic from LineupGrid so CSV/clipboard
  // exports match the current display order the user sees.
  const sortedLineups = useMemo(() => {
    if (!lineups.length) return lineups
    if (!gridSortConfig) return lineups

    const { key, dir } = gridSortConfig
    const mult = dir === 'desc' ? -1 : 1

    // Build grade map for grade-based sorting
    const gradeMap = {}
    lineups.forEach((lu, idx) => {
      if (lu.quality_score != null) {
        gradeMap[idx] = lu.quality_score
      }
    })
    if (analysis?.analyses) {
      for (const a of analysis.analyses) {
        gradeMap[a.lineup_index] = a.overall_score
      }
    }

    const indices = lineups.map((_, i) => i)
    indices.sort((a, b) => {
      let va, vb
      switch (key) {
        case 'grade':
          va = gradeMap[a] ?? -1
          vb = gradeMap[b] ?? -1
          break
        case 'proj':
          va = lineups[a].total_projected_fp
          vb = lineups[b].total_projected_fp
          break
        case 'floor':
          va = lineups[a].total_floor_fp
          vb = lineups[b].total_floor_fp
          break
        case 'ceil':
          va = lineups[a].total_ceiling_fp
          vb = lineups[b].total_ceiling_fp
          break
        case 'salary':
          va = lineups[a].total_salary
          vb = lineups[b].total_salary
          break
        default:
          return 0
      }
      if (va < vb) return -1 * mult
      if (va > vb) return 1 * mult
      return 0
    })

    return indices.map((i) => lineups[i])
  }, [lineups, gridSortConfig, analysis])

  // ── Export ────────────────────────────────────────────────────────
  const handleExport = () => {
    if (!lineups.length) return
    const ordered = sortedLineups

    let text
    if (isDK) {
      text = ordered
        .map((lu) => lu.players.map((p) => p.dk_player_id).join(','))
        .join('\n')
    } else {
      text = ordered
        .map((lu) =>
          lu.players.map((p) => `${p.roster_slot}:${p.player_name}:${p.salary}`).join('\n')
        )
        .join('\n\n')
    }

    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      copiedTimerRef.current = setTimeout(() => setCopied(false), 2000)
    })
  }

  const handleDownloadCSV = () => {
    if (!lineups.length) return
    const ordered = sortedLineups
    const firstLineup = ordered[0]
    const header = firstLineup.roster_slots
      ? firstLineup.roster_slots.join(',')
      : isDK
        ? 'PG,SG,SF,PF,C,G,F,UTIL'
        : 'PG,PG,SG,SG,SF,SF,PF,PF,C'
    const rows = ordered.map((lu) =>
      lu.players.map((p) => p.dk_player_id || p.player_id).join(',')
    )
    const csv = [header, ...rows].join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const exportDG = lineupDraftGroupId || draftGroupId
    const exportSlateName = lineupDraftGroupId
      ? (slates.find((s) => s.draft_group_id === lineupDraftGroupId)?.name || currentSlateName)
      : currentSlateName
    const slateSuffix = exportSlateName ? `_${exportSlateName.replace(/\s+/g, '')}` : ''
    const dgSuffix = exportDG ? `_DG${exportDG}` : ''
    a.download = `lineups_${platform}${slateSuffix}${dgSuffix}.csv`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  // ── Clear ────────────────────────────────────────────────────────
  const handleClear = () => {
    setLineups([])
    setBaselineScore(null)
    setBaselineLineup(null)
    setLineupDraftGroupId(null)
    setLockedPlayers(new Set())
    setExcludedPlayers(new Set())
    setAnalysis(null)
    setLastRefineResult(null)
  }

  // ── Lock / Exclude toggles ──────────────────────────────────────
  const toggleLock = (playerId) => {
    setLockedPlayers((prev) => {
      const next = new Set(prev)
      if (next.has(playerId)) next.delete(playerId)
      else {
        next.add(playerId)
        setExcludedPlayers((ex) => {
          const ne = new Set(ex)
          ne.delete(playerId)
          return ne
        })
      }
      return next
    })
  }

  const toggleExclude = (playerId) => {
    setExcludedPlayers((prev) => {
      const next = new Set(prev)
      if (next.has(playerId)) next.delete(playerId)
      else {
        next.add(playerId)
        setLockedPlayers((lk) => {
          const nl = new Set(lk)
          nl.delete(playerId)
          return nl
        })
      }
      return next
    })
  }

  // ── Projection edit handlers ────────────────────────────────────
  const handleProjectionEdit = useCallback((playerId, field, value) => {
    const numValue = parseFloat(value)
    if (isNaN(numValue) || numValue < 0) return
    const clamped = field === 'projected_minutes'
      ? Math.min(48, Math.max(0, numValue))
      : Math.max(0, numValue)
    const rounded = Math.round(clamped * 10) / 10

    setProjectionOverrides((prev) => ({
      ...prev,
      [playerId]: { ...(prev[playerId] || {}), [field]: rounded },
    }))
  }, [])

  const handleResetPlayer = useCallback((playerId) => {
    setProjectionOverrides((prev) => {
      const next = { ...prev }
      delete next[playerId]
      return next
    })
  }, [])

  const handleResetAll = useCallback(() => {
    setProjectionOverrides({})
  }, [])

  // ── Lineup player IDs for pool highlighting ─────────────────────
  const selectedLineup = lineups[selectedLineupIdx] || null
  const lineupPlayerIds = useMemo(
    () => new Set((selectedLineup?.players || []).map((p) => p.player_id)),
    [selectedLineup]
  )

  // ── Date label ──────────────────────────────────────────────────
  const todayStr = getLocalToday()
  const isToday = !selectedDate || selectedDate === todayStr

  // ── Imperative API for AI Chat integration ──────────────────────
  useImperativeHandle(ref, () => ({
    lockPlayer: (playerId) => {
      setLockedPlayers((prev) => {
        const next = new Set(prev); next.add(playerId); return next
      })
      setExcludedPlayers((prev) => {
        const next = new Set(prev); next.delete(playerId); return next
      })
    },
    unlockPlayer: (playerId) => {
      setLockedPlayers((prev) => {
        const next = new Set(prev); next.delete(playerId); return next
      })
    },
    excludePlayer: (playerId) => {
      setExcludedPlayers((prev) => {
        const next = new Set(prev); next.add(playerId); return next
      })
      setLockedPlayers((prev) => {
        const next = new Set(prev); next.delete(playerId); return next
      })
    },
    generate: (params) => handleGenerate(params || {}),
    analyze: () => handleAnalyze(),
    refine: () => handleRefine(),
    setStrategy: (s) => setStrategy(s),
    setPlatform: (p) => setPlatform(p),
    setContestType: (ct) => setContestType(ct),
    adjustProjections: (overrides) => {
      setProjectionOverrides((prev) => {
        const next = { ...prev }
        for (const [pid, fields] of Object.entries(overrides)) {
          next[pid] = { ...(next[pid] || {}), ...fields }
        }
        return next
      })
    },
    getContext: () => ({
      platform,
      activeSlate: currentSlateName,
      draftGroupId,
      strategy,
      contestType,
      numLineups,
      lockedPlayers: [...lockedPlayers],
      excludedPlayers: [...excludedPlayers],
      lineupCount: lineups.length,
      hasAnalysis: !!analysis,
      optimizing,
      analyzing,
      pool: originalPool,
      lineups,
      projectionOverrides,
    }),
    resolvePlayerName: (name) => {
      if (!name) return null
      const lower = name.toLowerCase().trim()
      const exact = originalPool.find((p) => p.player_name.toLowerCase() === lower)
      if (exact) return exact
      return originalPool.find((p) => {
        const pName = p.player_name.toLowerCase()
        return (
          pName.includes(lower) ||
          lower.includes(pName) ||
          lower.includes(pName.split(' ').pop())
        )
      }) || null
    },
  }))

  // ── Return everything the UI needs ──────────────────────────────
  return {
    // Platform & slate
    platform, setPlatform, activeSlate, setActiveSlate,
    // Pool
    pool: originalPool, displayPool, excludedPool, poolLoading, poolFetching, poolProgress, poolError,
    hasOverrides, projectionOverrides,
    // Lineups
    lineups, baselineScore, baselineLineup, selectedLineupIdx, setSelectedLineupIdx,
    numLineups, setNumLineups,
    strategy, setStrategy,
    contestType, setContestType,
    mode, setMode, showdownGameId, setShowdownGameId,
    maxExposure, setMaxExposure,
    recentWeight, setRecentWeight,
    optimalityThreshold, setOptimalityThreshold,
    deterministicMode, setDeterministicMode,
    enableStacking, setEnableStacking,
    primaryStackSize, setPrimaryStackSize,
    secondaryStackSize, setSecondaryStackSize,
    requireBringBack, setRequireBringBack,
    salaryFloorPct, setSalaryFloorPct,
    playerExposures, setPlayerExposures,
    optimizing, optimizeError, setOptimizeError,
    lineupDraftGroupId,
    // Analysis
    analysis, analyzing,
    // Refinement
    refining, refineSteps, lastRefineResult, setLastRefineResult,
    // Lock / Exclude
    lockedPlayers, excludedPlayers,
    // Export & grid sort
    copied, dkUploadOpen, setDkUploadOpen, dkExporterOpen, setDkExporterOpen,
    gridSortConfig, setGridSortConfig,
    // Progress
    optimizeSteps, analyzeSteps,
    // Derived
    hasSlates, slates, currentSlateName, currentSlate, draftGroupId, isDK,
    isLateSwap,
    slateMismatch, lineupSlateName, availableGames, selectedLineup, lineupPlayerIds,
    isToday,
    // Handlers
    handleGenerate, handleAnalyze, handleRefine,
    handleExport, handleDownloadCSV,
    handleClear,
    toggleLock, toggleExclude,
    handleProjectionEdit, handleResetPlayer, handleResetAll,
    handleRefreshPool,
  }
}
