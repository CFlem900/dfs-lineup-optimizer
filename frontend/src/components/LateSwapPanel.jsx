import React, { useState, useEffect, useCallback, useRef } from 'react';
import { AlertTriangle, Clock, RefreshCw, Shield, CheckCircle, XCircle, ArrowRightLeft } from 'lucide-react';

const API_BASE = '/api';

export default function LateSwapPanel({ gameDate, lineups, slateData, sport = 'nba' }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(false);

  const hasLineups = lineups && lineups.length > 0;

  // Stabilise lineups reference so useCallback doesn't re-create on every render
  const lineupsRef = useRef(lineups);
  lineupsRef.current = lineups;

  const fetchUpdates = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      let result;

      if (hasLineups && slateData) {
        // Full monitor with lineups — POST endpoint
        const res = await fetch(`${API_BASE}/late-swap/monitor/full`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            platform: slateData.platform || 'dk',
            draft_group_id: slateData.draft_group_id || slateData.draftGroupId || 0,
            game_date: gameDate || null,
            lineups: lineupsRef.current,
            sport: sport,
          }),
          credentials: 'include',
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        result = await res.json();
      } else {
        // Basic monitor — GET endpoint
        let params = gameDate ? `?game_date=${gameDate}` : '';
        if (sport !== 'nba') params += `${params ? '&' : '?'}sport=${sport}`;
        const res = await fetch(`${API_BASE}/late-swap/monitor${params}`, { credentials: 'include' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        result = await res.json();
      }

      setData(result);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [gameDate, hasLineups, slateData, sport]);

  useEffect(() => {
    fetchUpdates();
  }, [fetchUpdates]);

  // Keep a ref to the latest fetchUpdates so interval doesn't reset on dep changes
  const fetchUpdatesRef = useRef(fetchUpdates);
  useEffect(() => { fetchUpdatesRef.current = fetchUpdates; }, [fetchUpdates]);

  useEffect(() => {
    if (!autoRefresh) return;
    const interval = setInterval(() => fetchUpdatesRef.current(), 5 * 60 * 1000); // 5 min
    return () => clearInterval(interval);
  }, [autoRefresh]);

  // Determine which data shape we have (basic vs full)
  const injuryUpdates = data?.injury_updates || data?.recent_updates?.injury_updates || [];
  const newsUpdates = data?.news_updates || data?.recent_updates?.news_updates || [];
  const hasCritical = data?.has_critical_updates || data?.recent_updates?.has_critical_updates || data?.needs_attention || false;
  const atRiskPlayers = data?.at_risk_players || [];
  const swapSuggestions = data?.swap_suggestions || null;
  const checkedAt = data?.checked_at || data?.recent_updates?.checked_at;

  return (
    <div className="space-y-3 p-2">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 text-yellow-400" />
          <h3 className="text-white font-semibold text-sm">Late-Swap Monitor</h3>
          {hasLineups && (
            <span className="text-[10px] bg-blue-500/20 text-blue-400 px-1.5 py-0.5 rounded">
              {lineups.length} lineup{lineups.length > 1 ? 's' : ''}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1 text-xs text-gray-400 cursor-pointer">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              className="w-3 h-3"
            />
            Auto (5m)
          </label>
          <button
            onClick={fetchUpdates}
            disabled={loading}
            className="p-1 text-gray-400 hover:text-white transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      {error && (
        <div className="p-2 bg-red-500/10 border border-red-500/30 rounded text-red-400 text-xs">
          {error}
        </div>
      )}

      {data && (
        <>
          {/* Critical Alert Banner */}
          {hasCritical && (
            <div className="p-2 bg-red-500/20 border border-red-500/40 rounded-lg flex items-center gap-2">
              <XCircle className="w-4 h-4 text-red-400 shrink-0" />
              <span className="text-red-300 text-xs font-medium">
                Critical updates detected — players ruled Out
              </span>
            </div>
          )}

          {/* At-Risk Players (full monitor only) */}
          {atRiskPlayers.length > 0 && (
            <div className="bg-red-900/20 rounded-lg p-3 border border-red-700/40">
              <h4 className="text-red-300 text-xs font-semibold mb-2 flex items-center gap-1">
                <AlertTriangle className="w-3 h-3" /> At-Risk in Your Lineups ({atRiskPlayers.length})
              </h4>
              <div className="space-y-1.5 max-h-48 overflow-y-auto">
                {atRiskPlayers.map((p, i) => (
                  <div key={i} className="flex items-center justify-between text-xs py-1 border-b border-red-800/30 last:border-0">
                    <div className="flex items-center gap-2">
                      <span className="text-white font-medium">{p.player_name}</span>
                      <span className="text-gray-500">{p.position}</span>
                      <span className="text-gray-600">L{p.lineup_index + 1}</span>
                    </div>
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                      p.risk_level === 'high'
                        ? 'bg-red-500/20 text-red-400'
                        : 'bg-yellow-500/20 text-yellow-400'
                    }`}>
                      {p.injury_status}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Swap Suggestions (full monitor only) */}
          {swapSuggestions && swapSuggestions.total_swaps > 0 && (
            <div className="bg-blue-900/20 rounded-lg p-3 border border-blue-700/40">
              <h4 className="text-blue-300 text-xs font-semibold mb-2 flex items-center gap-1">
                <ArrowRightLeft className="w-3 h-3" /> Swap Suggestions ({swapSuggestions.total_swaps})
              </h4>
              <div className="space-y-1.5 max-h-48 overflow-y-auto">
                {(swapSuggestions.lineup_swaps || []).map((ls, li) =>
                  ls.swaps && ls.swaps.map((s, si) => (
                    <div key={`${li}-${si}`} className="text-xs py-1 border-b border-blue-800/30 last:border-0">
                      <div className="flex items-center gap-1">
                        <span className="text-gray-500">L{ls.lineup_index + 1}:</span>
                        <span className="text-red-400">{s.out_player_name || s.player_out || 'Player'}</span>
                        <span className="text-gray-600">→</span>
                        <span className="text-green-400">{s.in_player_name || s.player_in || 'Replacement'}</span>
                        {s.fp_change != null && (
                          <span className={`ml-1 ${s.fp_change > 0 ? 'text-green-500' : 'text-red-500'}`}>
                            ({s.fp_change > 0 ? '+' : ''}{typeof s.fp_change === 'number' ? s.fp_change.toFixed(1) : s.fp_change})
                          </span>
                        )}
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>
          )}

          {/* Injury Updates */}
          {injuryUpdates.length > 0 ? (
            <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700/50">
              <h4 className="text-gray-300 text-xs font-semibold mb-2 flex items-center gap-1">
                <Shield className="w-3 h-3" /> Injury Updates ({injuryUpdates.length})
              </h4>
              <div className="space-y-1.5 max-h-48 overflow-y-auto">
                {injuryUpdates.map((u, i) => (
                  <div key={i} className="flex items-center justify-between text-xs py-1 border-b border-gray-700/30 last:border-0">
                    <div className="flex items-center gap-2">
                      <span className="text-white font-medium">{u.player_name}</span>
                      <span className="text-gray-500">{u.team}</span>
                    </div>
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                      u.is_critical
                        ? 'bg-red-500/20 text-red-400'
                        : 'bg-yellow-500/20 text-yellow-400'
                    }`}>
                      {u.status}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : !atRiskPlayers.length && (
            <div className="p-3 bg-green-500/10 border border-green-500/20 rounded-lg flex items-center gap-2">
              <CheckCircle className="w-4 h-4 text-green-400" />
              <span className="text-green-300 text-xs">No at-risk injury updates</span>
            </div>
          )}

          {/* News Updates */}
          {newsUpdates.length > 0 && (
            <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700/50">
              <h4 className="text-gray-300 text-xs font-semibold mb-2 flex items-center gap-1">
                <Clock className="w-3 h-3" /> Recent News ({newsUpdates.length})
              </h4>
              <div className="space-y-1.5">
                {newsUpdates.map((n, i) => (
                  <div key={i} className="text-xs py-1 border-b border-gray-700/30 last:border-0">
                    <span className={n.is_positive ? 'text-green-400' : 'text-yellow-400'}>
                      {n.is_positive ? '\u2713' : '\u26A0'}
                    </span>{' '}
                    <span className="text-gray-300">{n.title}</span>
                    <span className="text-gray-600 ml-1">— {n.source}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Timestamp */}
          <div className="text-[10px] text-gray-600 text-right">
            Last checked: {checkedAt ? new Date(checkedAt).toLocaleTimeString() : 'N/A'}
          </div>
        </>
      )}
    </div>
  );
}
