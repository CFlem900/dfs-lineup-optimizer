import React from 'react'
import { HeartPulse, AlertTriangle, HelpCircle, Clock } from 'lucide-react'

function InjuryPanel({ injuries }) {
  const getStatusStyle = (status) => {
    switch (status) {
      case 'Out':
        return {
          bg: 'bg-red-900/20',
          border: 'border-red-800/50',
          text: 'text-red-400',
          icon: AlertTriangle,
        }
      case 'Doubtful':
        return {
          bg: 'bg-orange-900/20',
          border: 'border-orange-800/50',
          text: 'text-orange-400',
          icon: AlertTriangle,
        }
      case 'Questionable':
        return {
          bg: 'bg-yellow-900/20',
          border: 'border-yellow-800/50',
          text: 'text-yellow-400',
          icon: HelpCircle,
        }
      case 'GTD':
        return {
          bg: 'bg-blue-900/20',
          border: 'border-blue-800/50',
          text: 'text-blue-400',
          icon: Clock,
        }
      default:
        return {
          bg: 'bg-gray-900/20',
          border: 'border-gray-800/50',
          text: 'text-gray-400',
          icon: HelpCircle,
        }
    }
  }

  return (
    <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-ticker-border flex items-center gap-2">
        <HeartPulse className="w-4 h-4 text-ticker-red" />
        <h2 className="text-sm font-semibold uppercase tracking-wider">
          Injury Report
        </h2>
        {injuries.length > 0 && (
          <span className="ml-auto text-xs bg-ticker-red/20 text-ticker-red px-2 py-0.5 rounded-full">
            {injuries.length}
          </span>
        )}
      </div>

      {/* Injuries List */}
      <div className="divide-y divide-ticker-border/50">
        {injuries.length === 0 ? (
          <div className="px-4 py-6 text-center text-sm text-ticker-muted">
            No injuries reported
          </div>
        ) : (
          injuries.map((injury, idx) => {
            const style = getStatusStyle(injury.status)
            const StatusIcon = style.icon

            return (
              <div key={idx} className="px-4 py-3">
                <div className="flex items-start gap-3">
                  <StatusIcon className={`w-4 h-4 mt-0.5 ${style.text}`} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium truncate">
                        {injury.player_name}
                      </span>
                      <span
                        className={`text-xs px-2 py-0.5 rounded ${style.bg} ${style.border} border ${style.text}`}
                      >
                        {injury.status}
                      </span>
                    </div>
                    {injury.injury_description && (
                      <p className="text-xs text-ticker-muted mt-1">
                        {injury.injury_description}
                      </p>
                    )}
                  </div>
                </div>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}

export default InjuryPanel
