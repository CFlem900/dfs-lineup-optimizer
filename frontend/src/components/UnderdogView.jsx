/**
 * UnderdogView — Top-level container for Underdog Fantasy features.
 *
 * Renders the Pick'em view directly (Best Ball removed until implemented).
 */

import React from 'react'
import PickemView from './PickemView'

export default function UnderdogView({ selectedDate, sport = 'nba' }) {
  return (
    <div className="flex flex-col h-full">
      <PickemView selectedDate={selectedDate} sport={sport} />
    </div>
  )
}
