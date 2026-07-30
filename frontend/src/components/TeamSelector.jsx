import React, { useState, useEffect, useMemo } from 'react'
import { Search, ChevronDown, ChevronRight } from 'lucide-react'
import { rotationAPI } from '../services/api'

/**
 * Power conferences shown at top for quick access in CBB mode.
 */
const POWER_CONFERENCES = ['ACC', 'Big 12', 'Big East', 'Big Ten', 'SEC']

function TeamSelector({ teams, selectedTeam, onSelect, sport = 'nba' }) {
  const [search, setSearch] = useState('')
  const [isOpen, setIsOpen] = useState(false)
  const [conferences, setConferences] = useState(null)
  const [expandedConfs, setExpandedConfs] = useState(new Set(POWER_CONFERENCES))

  // Fetch conferences when sport is CBB
  useEffect(() => {
    if (sport === 'cbb' && isOpen && !conferences) {
      rotationAPI.getTeamsByConference('cbb')
        .then(data => setConferences(data.conferences || {}))
        .catch(() => setConferences(null))
    }
  }, [sport, isOpen, conferences])

  // Reset conferences when sport changes
  useEffect(() => {
    setConferences(null)
  }, [sport])

  const toggleConf = (conf) => {
    setExpandedConfs(prev => {
      const next = new Set(prev)
      if (next.has(conf)) next.delete(conf)
      else next.add(conf)
      return next
    })
  }

  // Filter logic
  const filteredTeams = useMemo(() => {
    if (!search) return teams
    const q = search.toLowerCase()
    return teams.filter(
      (t) =>
        t.full_name.toLowerCase().includes(q) ||
        t.abbreviation.toLowerCase().includes(q) ||
        (t.nickname && t.nickname.toLowerCase().includes(q))
    )
  }, [teams, search])

  // Grouped view for CBB (when not searching)
  const showGrouped = sport === 'cbb' && conferences && !search
  const filteredConferences = useMemo(() => {
    if (!showGrouped || !conferences) return null
    // Sort: power conferences first, then alphabetical
    const confNames = Object.keys(conferences)
    const power = confNames.filter(c => POWER_CONFERENCES.includes(c)).sort()
    const other = confNames.filter(c => !POWER_CONFERENCES.includes(c)).sort()
    return [...power, ...other]
  }, [showGrouped, conferences])

  return (
    <div className="relative" aria-label="Select team">
      <label className="block text-xs text-ticker-muted mb-1.5 uppercase tracking-wider">
        Select Team
      </label>

      {/* Dropdown Trigger */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full max-w-md flex items-center justify-between px-4 py-2.5
                   bg-ticker-card border border-ticker-border rounded-lg
                   hover:border-ticker-green/50 transition-colors text-left"
      >
        <span className={selectedTeam ? 'text-white' : 'text-ticker-muted'}>
          {selectedTeam ? selectedTeam.full_name : 'Choose a team...'}
        </span>
        <ChevronDown
          className={`w-4 h-4 text-ticker-muted transition-transform ${
            isOpen ? 'rotate-180' : ''
          }`}
        />
      </button>

      {/* Dropdown Panel */}
      {isOpen && (
        <div className="absolute z-20 mt-1 w-full max-w-md bg-ticker-card border border-ticker-border rounded-lg shadow-xl">
          {/* Search */}
          <div className="p-2 border-b border-ticker-border">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-ticker-muted" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={sport === 'cbb' ? 'Search 360+ teams...' : 'Search teams...'}
                className="w-full pl-9 pr-3 py-2 bg-ticker-bg border border-ticker-border rounded
                         text-sm text-white placeholder-ticker-muted focus:outline-none
                         focus:border-ticker-green/50"
                autoFocus
              />
            </div>
          </div>

          {/* Team List */}
          <div className="max-h-80 overflow-y-auto">
            {showGrouped && filteredConferences ? (
              // CBB: Conference-grouped view
              filteredConferences.map((conf) => {
                const confTeams = conferences[conf] || []
                const isExpanded = expandedConfs.has(conf)
                const isPower = POWER_CONFERENCES.includes(conf)
                return (
                  <div key={conf}>
                    <button
                      onClick={() => toggleConf(conf)}
                      className={`w-full px-3 py-2 text-left text-xs font-semibold uppercase tracking-wider
                                flex items-center justify-between
                                ${isPower ? 'text-ticker-green bg-ticker-green/5' : 'text-ticker-muted bg-ticker-bg/30'}
                                hover:bg-ticker-bg/50 transition-colors sticky top-0 z-10`}
                    >
                      <span>{conf} ({confTeams.length})</span>
                      {isExpanded ? (
                        <ChevronDown className="w-3 h-3" />
                      ) : (
                        <ChevronRight className="w-3 h-3" />
                      )}
                    </button>
                    {isExpanded && confTeams.map((team) => (
                      <button
                        key={team.id}
                        onClick={() => {
                          onSelect(team)
                          setIsOpen(false)
                          setSearch('')
                        }}
                        aria-label={team.full_name}
                        className={`w-full px-6 py-2 text-left text-sm hover:bg-ticker-bg/50
                                  transition-colors flex items-center justify-between
                                  ${selectedTeam?.id === team.id ? 'text-ticker-green bg-ticker-green/5' : 'text-gray-300'}`}
                      >
                        <span>{team.full_name}</span>
                        <span className="text-xs text-ticker-muted">{team.abbreviation}</span>
                      </button>
                    ))}
                  </div>
                )
              })
            ) : (
              // Flat list (NBA or CBB search results)
              <>
                {filteredTeams.map((team) => (
                  <button
                    key={team.id}
                    onClick={() => {
                      onSelect(team)
                      setIsOpen(false)
                      setSearch('')
                    }}
                    aria-label={team.full_name}
                    className={`w-full px-4 py-2.5 text-left text-sm hover:bg-ticker-bg/50
                              transition-colors flex items-center justify-between
                              ${selectedTeam?.id === team.id ? 'text-ticker-green bg-ticker-green/5' : 'text-gray-300'}`}
                  >
                    <span>{team.full_name}</span>
                    <span className="text-xs text-ticker-muted">{team.abbreviation}</span>
                  </button>
                ))}
                {filteredTeams.length === 0 && (
                  <div className="px-4 py-3 text-sm text-ticker-muted text-center">
                    No teams found
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {/* Click-away overlay */}
      {isOpen && (
        <div
          className="fixed inset-0 z-10"
          onClick={() => {
            setIsOpen(false)
            setSearch('')
          }}
        />
      )}
    </div>
  )
}

export default TeamSelector
