import React from 'react';

const SPORTS = [
  { value: 'nba', label: 'NBA', icon: null },
  { value: 'cbb', label: 'NCAA', icon: null },
  { value: 'nfl', label: 'NFL', icon: null },
  { value: 'mlb', label: 'MLB', icon: null },
];

/**
 * Sport toggle selector — NBA / NCAA toggle buttons.
 * Placed in the header bar alongside the view tabs.
 */
export default function SportSelector({ selectedSport, onSelect }) {
  return (
    <div className="flex items-center gap-1 bg-gray-800 rounded-lg p-0.5">
      {SPORTS.map(({ value, label }) => (
        <button
          key={value}
          onClick={() => onSelect(value)}
          className={`
            px-3 py-1 text-xs font-semibold rounded-md transition-all duration-150
            ${selectedSport === value
              ? 'bg-indigo-600 text-white shadow-sm'
              : 'text-gray-400 hover:text-gray-200 hover:bg-gray-700'
            }
          `}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
