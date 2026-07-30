import React, { useState, useEffect } from 'react';
import { BarChart3, TrendingUp, TrendingDown, Minus, RefreshCw, Target } from 'lucide-react';

const API_BASE = '/api';

async function fetchJson(url) {
  const res = await fetch(`${API_BASE}${url}`, { credentials: 'include' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function StatCard({ label, value, unit = '', color = 'blue', subtext = '' }) {
  const colorMap = {
    blue: 'text-blue-400 border-blue-500/30 bg-blue-500/10',
    green: 'text-green-400 border-green-500/30 bg-green-500/10',
    red: 'text-red-400 border-red-500/30 bg-red-500/10',
    yellow: 'text-yellow-400 border-yellow-500/30 bg-yellow-500/10',
    gray: 'text-gray-400 border-gray-500/30 bg-gray-500/10',
  };
  return (
    <div className={`p-3 rounded-lg border ${colorMap[color] || colorMap.blue}`}>
      <div className="text-xs text-gray-400 mb-1">{label}</div>
      <div className={`text-lg font-bold ${colorMap[color]?.split(' ')[0]}`}>
        {typeof value === 'number' ? value.toFixed(2) : value}{unit}
      </div>
      {subtext && <div className="text-xs text-gray-500 mt-0.5">{subtext}</div>}
    </div>
  );
}

function BiasIndicator({ bias }) {
  if (bias > 0.5) return <span className="text-red-400 flex items-center gap-1"><TrendingUp className="w-3 h-3" /> Over-projects</span>;
  if (bias < -0.5) return <span className="text-green-400 flex items-center gap-1"><TrendingDown className="w-3 h-3" /> Under-projects</span>;
  return <span className="text-gray-400 flex items-center gap-1"><Minus className="w-3 h-3" /> Neutral</span>;
}

function SimpleBarChart({ data, labelKey, valueKey, maxValue, color = 'bg-blue-500' }) {
  const max = maxValue || Math.max(...data.map(d => d[valueKey] || 0), 1);
  return (
    <div className="space-y-2">
      {data.map((item, i) => (
        <div key={i} className="flex items-center gap-2">
          <div className="w-10 text-xs text-gray-400 text-right shrink-0">{item[labelKey]}</div>
          <div className="flex-1 bg-gray-800 rounded-full h-5 relative overflow-hidden">
            <div
              className={`h-full rounded-full ${color} transition-all duration-500`}
              style={{ width: `${Math.min((item[valueKey] || 0) / max * 100, 100)}%` }}
            />
            <span className="absolute inset-0 flex items-center justify-center text-xs text-white font-medium">
              {(item[valueKey] || 0).toFixed(1)}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

export default function AccuracyDashboard({ sport = 'nba' }) {
  const [summary, setSummary] = useState(null);
  const [byPosition, setByPosition] = useState(null);
  const [bySalary, setBySalary] = useState(null);
  const [timeline, setTimeline] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [days, setDays] = useState(30);

  const sportParam = sport !== 'nba' ? `&sport=${sport}` : '';

  const loadData = async () => {
    setLoading(true);
    setError(null);
    try {
      const results = await Promise.allSettled([
        fetchJson(`/accuracy/summary?days=${days}${sportParam}`),
        fetchJson(`/accuracy/by-position?days=${days}${sportParam}`),
        fetchJson(`/accuracy/by-salary-tier?days=${days}${sportParam}`),
        fetchJson(`/accuracy/timeline?days=${Math.max(days, 30)}${sportParam}`),
      ]);

      const [sRes, pRes, salRes, tRes] = results;

      // Summary is required — if it failed, show the error
      if (sRes.status === 'rejected') {
        throw new Error(sRes.reason?.message || 'Failed to load accuracy summary');
      }
      setSummary(sRes.value);

      // Others are optional — use null fallback so dashboard still renders
      setByPosition(pRes.status === 'fulfilled' ? pRes.value : null);
      setBySalary(salRes.status === 'fulfilled' ? salRes.value : null);
      setTimeline(tRes.status === 'fulfilled' ? tRes.value : null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadData(); }, [days, sport]); // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="w-6 h-6 text-blue-400 animate-spin" />
        <span className="text-gray-400 ml-2">Loading accuracy data...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 bg-red-500/10 border border-red-500/30 rounded-lg text-center">
        <p className="text-red-400 text-sm mb-2">Failed to load accuracy data</p>
        <p className="text-gray-500 text-xs mb-3">{error}</p>
        <button onClick={loadData} className="px-3 py-1 bg-red-500/20 text-red-300 text-xs rounded hover:bg-red-500/30">
          Retry
        </button>
      </div>
    );
  }

  if (!summary || summary.records === 0) {
    return (
      <div className="p-6 text-center text-gray-400">
        <Target className="w-8 h-8 mx-auto mb-2 opacity-50" />
        <p className="text-sm">No historical accuracy data available yet.</p>
        <p className="text-xs text-gray-500 mt-1">Ingest box scores via the Backtest panel to start tracking.</p>
      </div>
    );
  }

  const posData = byPosition?.positions
    ? Object.entries(byPosition.positions).map(([pos, d]) => ({ pos, ...d }))
    : [];

  const salData = bySalary?.salary_tiers
    ? Object.entries(bySalary.salary_tiers).map(([tier, d]) => ({ tier: tier.charAt(0).toUpperCase() + tier.slice(1), ...d }))
    : [];

  const timelineData = timeline?.timeline || [];
  const recentTimeline = timelineData.slice(-14);

  return (
    <div className="space-y-4 p-2">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <BarChart3 className="w-5 h-5 text-blue-400" />
          <h2 className="text-white font-semibold text-sm">
            {sport === 'cbb' ? 'CBB' : 'NBA'} Projection Accuracy
          </h2>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="bg-gray-800 text-gray-300 text-xs px-2 py-1 rounded border border-gray-700"
          >
            <option value={7}>7 days</option>
            <option value={14}>14 days</option>
            <option value={30}>30 days</option>
            <option value={60}>60 days</option>
            <option value={90}>90 days</option>
          </select>
          <button onClick={loadData} className="p-1 text-gray-400 hover:text-white transition-colors">
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        <StatCard label="FP MAE" value={summary.fantasy_points?.mae} color="blue" subtext={`${summary.fantasy_points?.samples || 0} samples`} />
        <StatCard label="FP Bias" value={summary.fantasy_points?.bias} color={summary.fantasy_points?.bias > 0.5 ? 'red' : summary.fantasy_points?.bias < -0.5 ? 'green' : 'gray'} />
        <StatCard label="Minutes MAE" value={summary.minutes?.mae} unit=" min" color="yellow" />
        <StatCard label="Records" value={summary.records} color="gray" subtext={`${days} day window`} />
      </div>

      {/* Bias Direction */}
      <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700/50">
        <div className="text-xs text-gray-400 mb-1">Overall Bias Direction</div>
        <div className="text-sm">
          <BiasIndicator bias={summary.fantasy_points?.bias || 0} />
        </div>
      </div>

      {/* By Position */}
      {posData.length > 0 && (
        <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700/50">
          <h3 className="text-gray-300 text-xs font-semibold mb-2">FP MAE by Position</h3>
          <SimpleBarChart data={posData} labelKey="pos" valueKey="fp_mae" color="bg-blue-500" />
        </div>
      )}

      {/* By Salary Tier */}
      {salData.length > 0 && (
        <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700/50">
          <h3 className="text-gray-300 text-xs font-semibold mb-2">FP MAE by Salary Tier</h3>
          <SimpleBarChart data={salData} labelKey="tier" valueKey="fp_mae" color="bg-green-500" />
        </div>
      )}

      {/* Per-Stat Breakdown */}
      {summary.per_stat && Object.keys(summary.per_stat).length > 0 && (
        <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700/50">
          <h3 className="text-gray-300 text-xs font-semibold mb-2">Per-Stat MAE</h3>
          <SimpleBarChart
            data={Object.entries(summary.per_stat).map(([stat, d]) => ({ stat: stat.toUpperCase(), mae: d.mae }))}
            labelKey="stat"
            valueKey="mae"
            color="bg-purple-500"
          />
        </div>
      )}

      {/* Timeline (last 14 days) */}
      {recentTimeline.length > 0 && (
        <div className="bg-gray-800/50 rounded-lg p-3 border border-gray-700/50">
          <h3 className="text-gray-300 text-xs font-semibold mb-2">Daily FP MAE Trend (last 14 days)</h3>
          <div className="flex items-end gap-1 h-24">
            {recentTimeline.map((d, i) => {
              const maxMae = Math.max(...recentTimeline.map(t => t.fp_mae || 0), 1);
              const height = ((d.fp_mae || 0) / maxMae) * 100;
              return (
                <div key={i} className="flex-1 flex flex-col items-center" title={`${d.date}: ${d.fp_mae?.toFixed(1)}`}>
                  <div
                    className="w-full bg-blue-500/60 rounded-t hover:bg-blue-400 transition-colors"
                    style={{ height: `${height}%`, minHeight: '2px' }}
                  />
                  {i % 3 === 0 && (
                    <div className="text-[8px] text-gray-500 mt-1 whitespace-nowrap">
                      {d.date?.slice(5)}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
