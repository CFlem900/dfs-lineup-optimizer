import React, { useState } from 'react';
import { BarChart3, Play, Loader2, Trophy, TrendingUp, Target, DollarSign } from 'lucide-react';
import { rotationAPI } from '../services/api';

/**
 * Ownership Simulation Panel.
 *
 * Runs Monte Carlo simulations to estimate a lineup's expected
 * GPP performance (win rate, min-cash rate, ROI) against a field
 * generated from ownership projections.
 */
export default function OwnershipSimPanel({ lineup = null, sport = 'nba' }) {
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [fieldSize, setFieldSize] = useState(1000);
  const [numSims, setNumSims] = useState(500);

  const canSimulate = lineup?.players?.length > 0;

  const runSimulation = async () => {
    if (!lineup?.players?.length) return;

    setLoading(true);
    setError(null);
    try {
      const lineupData = lineup.players.map((p) => ({
        player_id: p.player_id || 0,
        player_name: p.player_name || p.name || '',
        projected_fp: p.projected_fp || p.dk_points || 0,
        ownership_pct: p.ownership_pct || p.ownership_projection || 5,
        ceiling_fp: p.ceiling_fp || (p.projected_fp || 0) * 1.3,
        floor_fp: p.floor_fp || (p.projected_fp || 0) * 0.5,
      }));

      const data = await rotationAPI.simulateOwnership({
        lineup: lineupData,
        fieldSize,
        numSimulations: numSims,
      });

      if (data.result) {
        setResult(data.result);
      } else {
        setError(data.error || 'Simulation returned no results');
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-sm font-medium text-gray-200">
          <BarChart3 className="w-4 h-4 text-blue-400" />
          Ownership Simulation
        </div>
      </div>

      {/* Controls */}
      <div className="flex items-center gap-3 mb-3">
        <div className="flex-1">
          <label className="text-xs text-gray-400 block mb-1">Field Size</label>
          <select
            value={fieldSize}
            onChange={(e) => setFieldSize(Number(e.target.value))}
            className="w-full bg-gray-700 border border-gray-600 rounded px-2 py-1 text-xs text-gray-200"
          >
            <option value={500}>500</option>
            <option value={1000}>1,000</option>
            <option value={5000}>5,000</option>
            <option value={10000}>10,000</option>
          </select>
        </div>
        <div className="flex-1">
          <label className="text-xs text-gray-400 block mb-1">Simulations</label>
          <select
            value={numSims}
            onChange={(e) => setNumSims(Number(e.target.value))}
            className="w-full bg-gray-700 border border-gray-600 rounded px-2 py-1 text-xs text-gray-200"
          >
            <option value={200}>200 (Fast)</option>
            <option value={500}>500 (Balanced)</option>
            <option value={1000}>1,000 (Precise)</option>
          </select>
        </div>
        <div className="pt-4">
          <button
            onClick={runSimulation}
            disabled={!canSimulate || loading}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium
              bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {loading ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Play className="w-3.5 h-3.5" />
            )}
            {loading ? 'Running...' : 'Simulate'}
          </button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="text-xs text-red-400 bg-red-900/20 border border-red-800 rounded px-3 py-2 mb-3">
          {error}
        </div>
      )}

      {/* No lineup */}
      {!canSimulate && !result && (
        <p className="text-xs text-gray-500 text-center py-3">
          Generate a lineup first, then run the simulation
        </p>
      )}

      {/* Results */}
      {result && (
        <div className="space-y-3">
          {/* Key Metrics Grid */}
          <div className="grid grid-cols-2 gap-2">
            <MetricCard
              icon={<Trophy className="w-4 h-4 text-yellow-400" />}
              label="Win Rate"
              value={`${result.win_rate}%`}
              sublabel={`${result.simulations_run} sims`}
            />
            <MetricCard
              icon={<Target className="w-4 h-4 text-green-400" />}
              label="Min Cash"
              value={`${result.min_cash_rate}%`}
              sublabel="Top 20%"
            />
            <MetricCard
              icon={<TrendingUp className="w-4 h-4 text-blue-400" />}
              label="Top 1%"
              value={`${result.top_1pct_rate}%`}
              sublabel="GPP upside"
            />
            <MetricCard
              icon={<DollarSign className="w-4 h-4 text-emerald-400" />}
              label="Exp ROI"
              value={`${result.expected_roi > 0 ? '+' : ''}${result.expected_roi}%`}
              sublabel={`Avg: ${result.avg_score} FP`}
              highlight={result.expected_roi > 0}
            />
          </div>

          {/* Score Distribution */}
          <div className="bg-gray-900/50 rounded p-3">
            <div className="text-xs text-gray-400 mb-2">Score Distribution</div>
            <div className="grid grid-cols-3 gap-2 text-xs">
              <div>
                <span className="text-gray-500">Your Avg</span>
                <div className="text-gray-200 font-medium">{result.avg_score}</div>
              </div>
              <div>
                <span className="text-gray-500">Field Avg</span>
                <div className="text-gray-200 font-medium">{result.field_avg_score}</div>
              </div>
              <div>
                <span className="text-gray-500">Std Dev</span>
                <div className="text-gray-200 font-medium">{result.score_std}</div>
              </div>
            </div>
          </div>

          {/* Benchmark Comparison */}
          {(result.chalk_comparison || result.contrarian_comparison) && (
            <div className="bg-gray-900/50 rounded p-3">
              <div className="text-xs text-gray-400 mb-2">vs Benchmarks</div>
              <div className="space-y-1.5 text-xs">
                {result.chalk_comparison && (
                  <div className="flex justify-between text-gray-300">
                    <span>Chalk (high own)</span>
                    <span>{result.chalk_comparison.projected_fp} FP / {result.chalk_comparison.avg_ownership}% own</span>
                  </div>
                )}
                {result.contrarian_comparison && (
                  <div className="flex justify-between text-gray-300">
                    <span>Contrarian (low own)</span>
                    <span>{result.contrarian_comparison.projected_fp} FP / {result.contrarian_comparison.avg_ownership}% own</span>
                  </div>
                )}
                <div className="flex justify-between text-gray-200 font-medium border-t border-gray-700 pt-1.5">
                  <span>Your Lineup</span>
                  <span>{result.avg_score} FP / {result.avg_percentile}th pctl</span>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function MetricCard({ icon, label, value, sublabel, highlight = false }) {
  return (
    <div className={`bg-gray-900/50 rounded p-2.5 ${highlight ? 'ring-1 ring-emerald-600/30' : ''}`}>
      <div className="flex items-center gap-1.5 mb-1">
        {icon}
        <span className="text-xs text-gray-400">{label}</span>
      </div>
      <div className={`text-lg font-semibold ${highlight ? 'text-emerald-400' : 'text-gray-100'}`}>
        {value}
      </div>
      {sublabel && <div className="text-xs text-gray-500">{sublabel}</div>}
    </div>
  );
}
