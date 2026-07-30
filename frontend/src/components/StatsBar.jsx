import React from 'react'
import { Clock, Users, BarChart3, Shield, Trophy } from 'lucide-react'

function StatsBar({ rotation }) {
  if (!rotation) return null

  const active = rotation.projections.filter((p) => p.adjusted_minutes > 0)
  const starters = [...rotation.projections]
    .sort((a, b) => b.adjusted_minutes - a.adjusted_minutes)
    .slice(0, 5)
  const starterMinutes = starters.reduce((s, p) => s + p.adjusted_minutes, 0)
  const benchMinutes = rotation.total_minutes - starterMinutes

  const avgConfidence =
    active.reduce((s, p) => s + p.confidence, 0) / (active.length || 1)

  const hasDFS = rotation.team_dk_total != null

  const stats = [
    {
      label: 'Total Minutes',
      value: rotation.total_minutes.toFixed(1),
      icon: Clock,
      color: 'text-ticker-green',
    },
    {
      label: 'Active Players',
      value: active.length,
      icon: Users,
      color: 'text-blue-400',
    },
    {
      label: 'Starter / Bench',
      value: `${starterMinutes.toFixed(0)} / ${benchMinutes.toFixed(0)}`,
      icon: BarChart3,
      color: 'text-yellow-400',
    },
    {
      label: 'Avg Confidence',
      value: `${(avgConfidence * 100).toFixed(0)}%`,
      icon: Shield,
      color: 'text-purple-400',
    },
  ]

  if (hasDFS) {
    stats.push({
      label: 'DK / FD Total',
      value: `${rotation.team_dk_total.toFixed(1)} / ${rotation.team_fd_total.toFixed(1)}`,
      icon: Trophy,
      color: 'text-orange-400',
    })
  }

  return (
    <div className={`grid grid-cols-2 ${hasDFS ? 'md:grid-cols-5' : 'md:grid-cols-4'} gap-3`}>
      {stats.map((stat) => (
        <div
          key={stat.label}
          className="bg-ticker-card border border-ticker-border rounded-lg px-4 py-3"
        >
          <div className="flex items-center gap-2 mb-1">
            <stat.icon className={`w-4 h-4 ${stat.color}`} />
            <span className="text-xs text-ticker-muted uppercase tracking-wider">
              {stat.label}
            </span>
          </div>
          <div className="text-lg font-bold">{stat.value}</div>
        </div>
      ))}
    </div>
  )
}

export default StatsBar
