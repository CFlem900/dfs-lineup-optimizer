import React, { useState, useEffect } from 'react';
import { TrendingDown, Zap, RefreshCw, Lock, XCircle } from 'lucide-react';

const API_BASE = '/api';

function FadeCard({ player, type, onAction }) {
  const isFade = type === 'fade';
  const borderColor = isFade ? 'border-red-500/30' : 'border-green-500/30';
  const bgColor = isFade ? 'bg-red-500/5' : 'bg-green-500/5';
  const score = isFade ? player.fade_score : player.leverage_score;

  return (
    <div className={`p-2 rounded-lg border ${borderColor} ${bgColor} transition-colors hover:bg-opacity-20`}>
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-1.5">
          <span className="text-white text-xs font-medium">{player.player_name}</span>
          <span className="text-gray-500 text-[10px]">{player.position} · {player.team}</span>
        </div>
        <span className={`text-[10px] font-bold ${isFade ? 'text-red-400' : 'text-green-400'}`}>
          {score?.toFixed(3)}
        </span>
      </div>
      <div className="flex items-center gap-3 text-[10px] text-gray-400 mb-1.5">
        <span>${player.salary?.toLocaleString()}</span>
        <span>{player.projected_fp} FP</span>
        <span>Ceil: {player.ceiling_fp}</span>
        <span className="font-medium text-yellow-400">{player.ownership_pct}% own</span>
      </div>
      <div className="text-[10px] text-gray-500 mb-1.5">{player.reason}</div>
      {onAction && (
        <div className="flex gap-1.5">
          {isFade ? (
            <button
              onClick={() => onAction('exclude', player.player_id)}
              className="flex items-center gap-1 px-1.5 py-0.5 bg-red-500/20 text-red-400 text-[10px] rounded hover:bg-red-500/30"
            >
              <XCircle className="w-2.5 h-2.5" /> Exclude
            </button>
          ) : (
            <button
              onClick={() => onAction('lock', player.player_id)}
              className="flex items-center gap-1 px-1.5 py-0.5 bg-green-500/20 text-green-400 text-[10px] rounded hover:bg-green-500/30"
            >
              <Lock className="w-2.5 h-2.5" /> Lock
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default function FadePanel({ onPlayerAction, sport = 'nba' }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const fetchFadeList = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/fade-list${sport !== 'nba' ? `?sport=${sport}` : ''}`, { credentials: 'include' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const result = await res.json();
      setData(result);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchFadeList(); }, [sport]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleAction = (action, playerId) => {
    if (onPlayerAction) {
      onPlayerAction(action, playerId);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center p-4">
        <RefreshCw className="w-4 h-4 text-blue-400 animate-spin mr-2" />
        <span className="text-gray-400 text-xs">Loading fade list...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-3 bg-red-500/10 border border-red-500/30 rounded text-center">
        <p className="text-red-400 text-xs mb-1">{error}</p>
        <button onClick={fetchFadeList} className="text-xs text-red-300 hover:text-red-200 underline">
          Retry
        </button>
      </div>
    );
  }

  if (!data || ((!data.fades || data.fades.length === 0) && (!data.leverage_plays || data.leverage_plays.length === 0))) {
    return (
      <div className="p-4 text-center text-gray-500 text-xs">
        {data?.message || 'No fade/leverage data available. Build a player pool with ownership projections first.'}
      </div>
    );
  }

  return (
    <div className="space-y-3 p-2">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h3 className="text-white font-semibold text-sm">Contrarian Analysis</h3>
        <button onClick={fetchFadeList} className="p-1 text-gray-400 hover:text-white">
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      {/* Summary */}
      <div className="flex gap-2 text-[10px] text-gray-500">
        <span>Pool: {data.pool_size}</span>
        <span>·</span>
        <span>With ownership: {data.players_with_ownership}</span>
        <span>·</span>
        <span>Median ceiling: {data.median_ceiling}</span>
      </div>

      {/* Two-column layout */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {/* Fades */}
        <div>
          <h4 className="flex items-center gap-1 text-red-400 text-xs font-semibold mb-2">
            <TrendingDown className="w-3 h-3" /> Fades ({data.fades?.length || 0})
          </h4>
          <div className="space-y-1.5 max-h-64 overflow-y-auto">
            {(data.fades || []).map((p, i) => (
              <FadeCard key={i} player={p} type="fade" onAction={handleAction} />
            ))}
          </div>
        </div>

        {/* Leverage Plays */}
        <div>
          <h4 className="flex items-center gap-1 text-green-400 text-xs font-semibold mb-2">
            <Zap className="w-3 h-3" /> Leverage Plays ({data.leverage_plays?.length || 0})
          </h4>
          <div className="space-y-1.5 max-h-64 overflow-y-auto">
            {(data.leverage_plays || []).map((p, i) => (
              <FadeCard key={i} player={p} type="leverage" onAction={handleAction} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
