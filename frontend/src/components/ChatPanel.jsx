/**
 * ChatPanel — Sidebar AI assistant for natural language DFS interaction.
 *
 * Styled to match ExpertPanel / NewsPanel (bg-ticker-card, border-ticker-border).
 * Supports SSE streaming responses and actionable command buttons.
 */

import React, { useState, useRef, useEffect, useCallback } from 'react'
import { Bot, Send, Trash2, ChevronDown, ChevronUp, Zap, Loader2 } from 'lucide-react'
import useChat from '../hooks/useChat'

const ACTION_LABELS = {
  lock_player: '🔒 Lock',
  exclude_player: '❌ Exclude',
  unlock_player: '🔓 Unlock',
  generate_lineups: '⚡ Generate',
  set_strategy: '🎯 Strategy',
  set_platform: '📱 Platform',
  set_contest_type: '🏆 Contest',
  analyze_lineups: '📊 Analyze',
  refine_lineups: '✨ Refine',
  adjust_projections: '📐 Adjust',
}

export default function ChatPanel({ collapsed = false, onAction, context = {} }) {
  const { messages, streaming, error, sendMessage, clearChat, cancelStream } = useChat()
  const [input, setInput] = useState('')
  const [isCollapsed, setIsCollapsed] = useState(collapsed)
  const messagesEndRef = useRef(null)
  const inputRef = useRef(null)

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Cancel any in-flight stream on unmount
  useEffect(() => {
    return () => {
      cancelStream()
    }
  }, [cancelStream])

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!input.trim() || streaming) return

    const text = input
    setInput('')
    // Resolve context at send-time for freshest state
    const currentContext = typeof context === 'function' ? context() : context
    const result = await sendMessage(text, currentContext)

    // Execute actions via parent callback
    if (result.actions?.length > 0 && onAction) {
      result.actions.forEach((action) => onAction(action))
    }
  }

  const handleActionClick = (action) => {
    if (onAction) onAction(action)
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit(e)
    }
  }

  if (isCollapsed) {
    return (
      <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden">
        <button
          onClick={() => setIsCollapsed(false)}
          className="w-full px-4 py-3 flex items-center justify-between hover:bg-ticker-bg/30 transition-colors"
        >
          <div className="flex items-center gap-2">
            <Bot className="w-4 h-4 text-ticker-green" />
            <h3 className="text-sm font-semibold uppercase tracking-wider">AI Assistant</h3>
            {messages.length > 0 && (
              <span className="px-1.5 py-0.5 bg-ticker-green/10 text-ticker-green text-[10px] font-bold rounded-full">
                {messages.length}
              </span>
            )}
          </div>
          <ChevronDown className="w-3.5 h-3.5 text-ticker-muted" />
        </button>
      </div>
    )
  }

  return (
    <div className="bg-ticker-card border border-ticker-border rounded-lg overflow-hidden flex flex-col max-h-[600px]">
      {/* Header */}
      <div className="px-4 py-3 border-b border-ticker-border flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-2">
          <Bot className="w-4 h-4 text-ticker-green" />
          <h3 className="text-sm font-semibold uppercase tracking-wider">AI Assistant</h3>
        </div>
        <div className="flex items-center gap-2">
          {messages.length > 0 && (
            <button
              onClick={clearChat}
              className="p-1 text-ticker-muted hover:text-red-400 transition-colors"
              title="Clear chat"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          )}
          <button
            onClick={() => setIsCollapsed(true)}
            className="p-1 text-ticker-muted hover:text-white transition-colors"
          >
            <ChevronUp className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-3 min-h-[200px]" aria-live="polite">
        {messages.length === 0 && (
          <div className="text-center py-6">
            <Bot className="w-8 h-8 text-ticker-border mx-auto mb-2" />
            <p className="text-xs text-ticker-muted">
              Ask about players, build lineups, or get strategy advice.
            </p>
            <div className="mt-3 space-y-1">
              {['Lock LeBron James', 'Build 5 ceiling lineups', 'How does Jokic look tonight?'].map(
                (suggestion) => (
                  <button
                    key={suggestion}
                    onClick={() => {
                      setInput(suggestion)
                      inputRef.current?.focus()
                    }}
                    className="block w-full text-left px-3 py-1.5 text-[11px] text-ticker-muted
                             bg-ticker-bg/50 rounded hover:bg-ticker-bg hover:text-white transition-colors"
                  >
                    &ldquo;{suggestion}&rdquo;
                  </button>
                )
              )}
            </div>
          </div>
        )}

        {messages.map((msg, idx) => (
          <div
            key={`${msg.role}-${msg.timestamp}-${idx}`}
            className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
          >
            <div
              className={`max-w-[85%] rounded-lg px-3 py-2 text-xs leading-relaxed ${
                msg.role === 'user'
                  ? 'bg-ticker-green/20 text-white ml-4'
                  : msg.isError
                  ? 'bg-red-900/20 text-red-300 mr-4'
                  : 'bg-ticker-bg/70 text-gray-200 mr-4'
              }`}
            >
              {msg.content || (streaming && idx === messages.length - 1 ? (
                <span className="flex items-center gap-1 text-ticker-muted">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  Thinking...
                </span>
              ) : null)}

              {/* Action buttons from AI response */}
              {msg.actions?.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {msg.actions.map((action, i) => (
                    <button
                      key={`${action.type}-${i}`}
                      onClick={() => handleActionClick(action)}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded text-[10px]
                               font-semibold bg-ticker-green/20 text-ticker-green
                               hover:bg-ticker-green/30 transition-colors"
                    >
                      <Zap className="w-2.5 h-2.5" />
                      {action.label || ACTION_LABELS[action.type] || action.type}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}

        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <form
        onSubmit={handleSubmit}
        className="px-3 py-2 border-t border-ticker-border flex-shrink-0"
      >
        <div className="flex items-center gap-2">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={streaming ? 'Waiting for response...' : 'Ask about lineups, players...'}
            disabled={streaming}
            aria-label="Type a message"
            className="flex-1 bg-ticker-bg border border-ticker-border rounded-md px-3 py-1.5
                     text-xs text-white placeholder-ticker-muted
                     focus:outline-none focus:border-ticker-green/50
                     disabled:opacity-50 disabled:cursor-not-allowed"
          />
          <button
            type="submit"
            disabled={streaming || !input.trim()}
            aria-label="Send message"
            className="p-1.5 bg-ticker-green/20 text-ticker-green rounded-md
                     hover:bg-ticker-green/30 transition-colors
                     disabled:opacity-30 disabled:cursor-not-allowed"
          >
            {streaming ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Send className="w-4 h-4" />
            )}
          </button>
        </div>
        {error && (
          <p className="text-[10px] text-red-400 mt-1">{error}</p>
        )}
      </form>
    </div>
  )
}
