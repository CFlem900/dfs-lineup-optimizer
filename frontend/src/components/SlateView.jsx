import React, { useState, useEffect, useRef, useMemo } from 'react'
import { Tv, RefreshCw, Calendar, Clock, Sun, Moon, Radio } from 'lucide-react'
import GameSlateCard from './GameSlateCard'
import SlatePlayersPanel from './SlatePlayersPanel'
import ValuePlaysPanel from './ValuePlaysPanel'
import PropsComparisonPanel from './PropsComparisonPanel'
import { getLocalToday } from '../utils/dateUtils'

const SLATE_ICONS = {
  Early: Sun,
  Main: Tv,
  Night: Moon,
  'All Games': Tv,
}

const SLATE_ACTIVE_COLORS = {
  Early: 'bg-yellow-500 text-black',
  Main: 'bg-ticker-green text-white',
  Night: 'bg-purple-500 text-white',
  'All Games': 'bg-ticker-green text-white',
}

function SlateView({
  slate,
  loading,
  onOpenTeamModal,
  onSimulate,
  playersData,
  playersLoading,
  playersError,
  onRetryPlayers,
  onSlateChange,
  selectedDate,
  sport = 'nba',
}) {
  const [activeSlate, setActiveSlate] = useState(null)

  // Keep a stable ref to the callback so the effect below doesn't
  // re-fire every time the parent recreates handleSlateChange.
  const onSlateChangeRef = useRef(onSlateChange)
  useEffect(() => { onSlateChangeRef.current = onSlateChange }, [onSlateChange])

  // Reset active slate when slate data changes (e.g. date or sport switch)
  useEffect(() => { setActiveSlate(null) }, [slate])

  // Determine if we're viewing today's date
  const todayStr = getLocalToday()
  const isToday = !selectedDate || selectedDate === todayStr
  const dateLabel = isToday
    ? "Today's"
    : new Date(selectedDate + 'T12:00:00').toLocaleDateString('en-US', {
        weekday: 'short',
        month: 'short',
        day: 'numeric',
      })

  const slates = slate?.slates?.length > 0 ? slate.slates : null
  const currentSlateName = activeSlate || (slates ? slates[0].name : null)

  const { currentSlate, displayGames, draftGroupId } = useMemo(() => {
    const found = slates?.find((s) => s.name === currentSlateName) || null
    // When no named slate matches, fall back to the first slate's
    // draft_group_id (if available) so DK salaries still attach.
    const dgId = found
      ? found.draft_group_id
      : slates?.[0]?.draft_group_id || null
    return {
      currentSlate: found,
      displayGames: found ? found.games : (slate ? slate.games : []),
      draftGroupId: dgId,
    }
  }, [slates, currentSlateName, slate])

  // Notify parent when the active slate (and its games) change.
  // Uses a ref for the callback so that parent identity changes
  // (e.g. from selectedDate updates) don't cause missed or
  // duplicate fetches.  The effect uses a stable string key derived
  // from the game IDs + draftGroupId so that new array references
  // with identical content don't trigger spurious re-fetches.
  const slateKey = useMemo(() => {
    if (!displayGames.length) return ''
    const ids = displayGames.map(g => g.game_id || `${g.away_team?.team_id}-${g.home_team?.team_id}`)
    return `${ids.join(',')}_${draftGroupId || ''}`
  }, [displayGames, draftGroupId])

  useEffect(() => {
    if (onSlateChangeRef.current && slateKey && displayGames.length > 0) {
      onSlateChangeRef.current(displayGames, draftGroupId)
    }
  }, [slateKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleSlateClick = (name) => {
    setActiveSlate(name)
  }

  if (loading) {
    return (
      <div className="mt-8 flex flex-col items-center gap-3">
        <RefreshCw className="w-8 h-8 text-ticker-green animate-spin" />
        <p className="text-sm text-ticker-muted">
          {isToday ? "Loading today\u2019s slate..." : "Loading slate..."}
        </p>
      </div>
    )
  }

  if (!slate || slate.game_count === 0) {
    return (
      <div className="mt-16 text-center">
        <Calendar className="w-16 h-16 text-ticker-border mx-auto mb-4" />
        <h2 className="text-lg font-semibold text-gray-400 mb-2">
          {isToday ? 'No Games Today' : `No Games on ${dateLabel}`}
        </h2>
        <p className="text-sm text-ticker-muted max-w-md mx-auto">
          {isToday
            ? `There are no ${sport === 'cbb' ? 'college basketball' : 'NBA'} games scheduled for today. Check back on a game day to see the full slate with projections.`
            : `There are no ${sport === 'cbb' ? 'college basketball' : 'NBA'} games scheduled for this date. Try selecting a different date.`}
        </p>
      </div>
    )
  }

  return (
    <div className="mt-6 space-y-5">
      {/* Header row */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Tv className="w-5 h-5 text-ticker-green" />
          <h2 className="text-sm font-semibold uppercase tracking-wider">
            {isToday ? "Today\u2019s Slate" : `${dateLabel} Slate`}
          </h2>
          <span className="px-2 py-0.5 bg-ticker-green/10 text-ticker-green text-xs font-bold rounded-full">
            {slate.game_count} {slate.game_count === 1 ? 'Game' : 'Games'}
          </span>
        </div>
      </div>

      {/* Slate Tabs */}
      {slates && slates.length > 0 && (
        <div className="flex items-center gap-1 bg-ticker-card border border-ticker-border rounded-lg p-1">
          {slates.map((dfsSlate) => {
            const isLive = dfsSlate.is_live || false
            const lateSwapActive = dfsSlate.late_swap_active || false
            const SlateIcon = isLive ? Radio : (SLATE_ICONS[dfsSlate.name] || Clock)
            const isActive = dfsSlate.name === currentSlateName
            const activeColor = isLive
              ? 'bg-red-600 text-white'
              : (SLATE_ACTIVE_COLORS[dfsSlate.name] || 'bg-ticker-green text-white')

            return (
              <button
                key={dfsSlate.name}
                onClick={() => handleSlateClick(dfsSlate.name)}
                className={`flex items-center gap-2 px-4 py-2 text-xs font-semibold rounded-md transition-colors flex-1 justify-center ${
                  isActive
                    ? activeColor
                    : isLive
                      ? 'text-red-400 hover:text-red-300 hover:bg-red-900/20'
                      : 'text-ticker-muted hover:text-white hover:bg-ticker-bg'
                }`}
              >
                <SlateIcon className={`w-3.5 h-3.5 ${isLive && isActive ? 'animate-pulse' : ''}`} />
                <span>{isLive ? 'Live' : dfsSlate.name}</span>
                {lateSwapActive && (
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                    isActive ? 'bg-white/20' : 'bg-red-900/30 text-red-400'
                  }`}>
                    Late Swap
                  </span>
                )}
                <span
                  className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                    isActive
                      ? 'bg-white/20'
                      : 'bg-ticker-bg'
                  }`}
                >
                  {dfsSlate.game_count}
                </span>
              </button>
            )
          })}
        </div>
      )}

      {/* Active slate label */}
      {currentSlate && currentSlate.label && (
        <div className="flex items-center gap-2 text-xs text-ticker-muted">
          <span>{currentSlate.label}</span>
          {currentSlate.is_live && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-red-900/30 text-red-400 rounded-full text-[10px] font-bold uppercase tracking-wider">
              <span className="w-1.5 h-1.5 bg-red-500 rounded-full animate-pulse" />
              {currentSlate.late_swap_active ? 'Live — Late Swap Active' : 'Live'}
            </span>
          )}
        </div>
      )}

      {/* Three-column layout: Player Pool (left) | Game Cards (center) | Value Plays (right) */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
        {/* Player Pool Panel */}
        <div className="lg:col-span-4">
          <SlatePlayersPanel
            playersData={playersData}
            loading={playersLoading}
            error={playersError}
            onRetry={onRetryPlayers}
            sport={sport}
          />
        </div>

        {/* Game Cards Grid */}
        <div className="lg:col-span-5">
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            {displayGames.map((game) => (
              <GameSlateCard
                key={game.game_id}
                game={game}
                sport={sport}
                onOpenTeamModal={onOpenTeamModal}
                onSimulate={onSimulate}
              />
            ))}
          </div>
        </div>

        {/* Value Plays + Props Panel */}
        <div className="lg:col-span-3 space-y-4">
          <ValuePlaysPanel
            playersData={playersData}
            playersLoading={playersLoading}
            games={displayGames}
          />
          <PropsComparisonPanel
            playersData={playersData}
            selectedDate={selectedDate}
            sport={sport}
          />
        </div>
      </div>
    </div>
  )
}

export default SlateView
