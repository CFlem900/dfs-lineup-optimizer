import React, { useState, useMemo } from 'react';
import { ArrowLeftRight, TrendingUp, TrendingDown, Minus } from 'lucide-react';

function ValueDiff({ diff }) {
  if (Math.abs(diff) < 0.1) return <Minus className="w-3 h-3 text-gray-500" />;
  if (diff > 0) return <TrendingUp className="w-3 h-3 text-green-400" />;
  return <TrendingDown className="w-3 h-3 text-red-400" />;
}

export default function SlateComparison({ slateA, slateB, poolA = [], poolB = [] }) {
  const [sortBy, setSortBy] = useState('valueDiff');
  const [sortDir, setSortDir] = useState('desc');

  const comparison = useMemo(() => {
    if (!poolA.length || !poolB.length) return [];

    // Build lookup for pool B by player_name
    const bLookup = {};
    for (const p of poolB) {
      bLookup[p.player_name?.toLowerCase()] = p;
    }

    const rows = [];
    for (const a of poolA) {
      const b = bLookup[a.player_name?.toLowerCase()];
      if (!b) continue;

      const valueA = a.salary > 0 ? (a.projected_fp / a.salary) * 1000 : 0;
      const valueB = b.salary > 0 ? (b.projected_fp / b.salary) * 1000 : 0;

      rows.push({
        player_name: a.player_name,
        position: a.position,
        team: a.team_abbreviation,
        salaryA: a.salary,
        salaryB: b.salary,
        salaryDiff: b.salary - a.salary,
        projA: a.projected_fp,
        projB: b.projected_fp,
        valueA: Math.round(valueA * 100) / 100,
        valueB: Math.round(valueB * 100) / 100,
        valueDiff: Math.round((valueA - valueB) * 100) / 100,
        ceilingA: a.ceiling_fp || 0,
        ceilingB: b.ceiling_fp || 0,
      });
    }

    // Sort
    rows.sort((a, b) => {
      const aVal = a[sortBy] || 0;
      const bVal = b[sortBy] || 0;
      return sortDir === 'desc' ? bVal - aVal : aVal - bVal;
    });

    return rows;
  }, [poolA, poolB, sortBy, sortDir]);

  const toggleSort = (field) => {
    if (sortBy === field) {
      setSortDir(d => d === 'desc' ? 'asc' : 'desc');
    } else {
      setSortBy(field);
      setSortDir('desc');
    }
  };

  if (!poolA.length || !poolB.length) {
    return (
      <div className="p-4 text-center text-gray-500 text-xs">
        Select two slates and build pools for both to compare.
      </div>
    );
  }

  const betterOnA = comparison.filter(r => r.valueDiff > 0.2).length;
  const betterOnB = comparison.filter(r => r.valueDiff < -0.2).length;

  return (
    <div className="space-y-3 p-2">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <ArrowLeftRight className="w-4 h-4 text-blue-400" />
          <h3 className="text-white font-semibold text-sm">Slate Comparison</h3>
        </div>
        <div className="text-xs text-gray-400">
          {comparison.length} players in common
        </div>
      </div>

      {/* Summary */}
      <div className="flex gap-3 text-xs">
        <div className="px-2 py-1 bg-green-500/10 border border-green-500/20 rounded text-green-400">
          {betterOnA} better on {slateA?.name || 'Slate A'}
        </div>
        <div className="px-2 py-1 bg-blue-500/10 border border-blue-500/20 rounded text-blue-400">
          {betterOnB} better on {slateB?.name || 'Slate B'}
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-400 border-b border-gray-700">
              <th className="text-left py-1.5 px-1">Player</th>
              <th className="text-right py-1.5 px-1 cursor-pointer hover:text-white" onClick={() => toggleSort('salaryA')}>
                Sal A
              </th>
              <th className="text-right py-1.5 px-1 cursor-pointer hover:text-white" onClick={() => toggleSort('salaryB')}>
                Sal B
              </th>
              <th className="text-right py-1.5 px-1 cursor-pointer hover:text-white" onClick={() => toggleSort('salaryDiff')}>
                Δ Sal
              </th>
              <th className="text-right py-1.5 px-1 cursor-pointer hover:text-white" onClick={() => toggleSort('valueA')}>
                Val A
              </th>
              <th className="text-right py-1.5 px-1 cursor-pointer hover:text-white" onClick={() => toggleSort('valueB')}>
                Val B
              </th>
              <th className="text-right py-1.5 px-1 cursor-pointer hover:text-white" onClick={() => toggleSort('valueDiff')}>
                Δ Val {sortBy === 'valueDiff' && (sortDir === 'desc' ? '↓' : '↑')}
              </th>
            </tr>
          </thead>
          <tbody>
            {comparison.slice(0, 50).map((row, i) => (
              <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="py-1.5 px-1">
                  <span className="text-white font-medium">{row.player_name}</span>
                  <span className="text-gray-500 ml-1">{row.position}</span>
                </td>
                <td className="text-right py-1.5 px-1 text-gray-300">${row.salaryA?.toLocaleString()}</td>
                <td className="text-right py-1.5 px-1 text-gray-300">${row.salaryB?.toLocaleString()}</td>
                <td className={`text-right py-1.5 px-1 ${
                  row.salaryDiff > 0 ? 'text-red-400' : row.salaryDiff < 0 ? 'text-green-400' : 'text-gray-500'
                }`}>
                  {row.salaryDiff > 0 ? '+' : ''}{row.salaryDiff?.toLocaleString()}
                </td>
                <td className="text-right py-1.5 px-1 text-gray-300">{row.valueA}</td>
                <td className="text-right py-1.5 px-1 text-gray-300">{row.valueB}</td>
                <td className="text-right py-1.5 px-1">
                  <span className="flex items-center justify-end gap-1">
                    <ValueDiff diff={row.valueDiff} />
                    <span className={
                      row.valueDiff > 0.2 ? 'text-green-400' : row.valueDiff < -0.2 ? 'text-red-400' : 'text-gray-500'
                    }>
                      {row.valueDiff > 0 ? '+' : ''}{row.valueDiff}
                    </span>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
