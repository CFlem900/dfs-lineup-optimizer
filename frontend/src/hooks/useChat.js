/**
 * useChat — Custom hook for managing AI chat state and SSE streaming.
 *
 * Manages session ID, message history, streaming state, and action
 * execution.  Calls the backend /chat/stream endpoint for real-time
 * token-by-token responses.
 */

import { useState, useRef, useCallback } from 'react'

const API_BASE = import.meta.env.VITE_API_URL || '/api'

export default function useChat() {
  const [messages, setMessages] = useState([])
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState(null)
  const sessionIdRef = useRef(null)
  const abortRef = useRef(null)

  /**
   * Send a message and stream the response.
   *
   * @param {string} text - User message
   * @param {object} context - Current app state (platform, locked players, etc.)
   * @returns {Promise<{actions: Array}>} Parsed actions from the response
   */
  const sendMessage = useCallback(async (text, context = {}) => {
    if (!text.trim() || streaming) return { actions: [] }

    setError(null)

    // Add user message immediately
    const userMsg = { role: 'user', content: text, timestamp: Date.now() }
    setMessages((prev) => [...prev, userMsg])

    // Create placeholder for assistant response
    const assistantMsg = { role: 'assistant', content: '', timestamp: Date.now() }
    setMessages((prev) => [...prev, assistantMsg])

    setStreaming(true)

    try {
      const body = {
        message: text,
        session_id: sessionIdRef.current,
        context,
      }

      // Abort any in-flight request before starting a new one
      if (abortRef.current) abortRef.current.abort()
      abortRef.current = new AbortController()

      const res = await fetch(`${API_BASE}/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: abortRef.current.signal,
        credentials: 'include',
      })

      if (!res.ok) {
        throw new Error('Chat request failed')
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let fullText = ''
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })

        // Parse SSE events from buffer
        const lines = buffer.split('\n')
        buffer = lines.pop() || '' // Keep incomplete line in buffer

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6)
            if (data === '[DONE]') continue

            try {
              const parsed = JSON.parse(data)

              // Handle different SSE event types
              if (parsed.session_id) {
                sessionIdRef.current = parsed.session_id
              }
              if (parsed.chunk) {
                fullText += parsed.chunk
                // Update the last message (assistant placeholder)
                setMessages((prev) => {
                  const updated = [...prev]
                  updated[updated.length - 1] = {
                    ...updated[updated.length - 1],
                    content: fullText,
                  }
                  return updated
                })
              }
              if (parsed.error) {
                setError(parsed.error)
                setMessages((prev) => {
                  const updated = [...prev]
                  updated[updated.length - 1] = {
                    ...updated[updated.length - 1],
                    content: fullText || `Error: ${parsed.error}`,
                    isError: !fullText,
                  }
                  return updated
                })
                setStreaming(false)
                return { actions: [] }
              }
              if (parsed.done || parsed.actions) {
                // Final event — may include parsed actions
                const actions = parsed.actions || []
                if (actions.length > 0) {
                  setMessages((prev) => {
                    const updated = [...prev]
                    updated[updated.length - 1] = {
                      ...updated[updated.length - 1],
                      actions,
                    }
                    return updated
                  })
                }
                setStreaming(false)
                return { actions }
              }
            } catch {
              // Plain text chunk (non-JSON SSE)
              fullText += data
              setMessages((prev) => {
                const updated = [...prev]
                updated[updated.length - 1] = {
                  ...updated[updated.length - 1],
                  content: fullText,
                }
                return updated
              })
            }
          }
        }
      }

      setStreaming(false)
      return { actions: [] }
    } catch (err) {
      setError(err.message)
      setStreaming(false)
      // Update the assistant message with error
      setMessages((prev) => {
        const updated = [...prev]
        if (updated.length > 0 && updated[updated.length - 1].role === 'assistant') {
          updated[updated.length - 1] = {
            ...updated[updated.length - 1],
            content: 'Sorry, I had trouble connecting. Please try again.',
            isError: true,
          }
        }
        return updated
      })
      return { actions: [] }
    }
  }, [streaming])

  /**
   * Send a non-streaming message (fallback).
   */
  const sendMessageSync = useCallback(async (text, context = {}) => {
    if (!text.trim()) return { actions: [] }

    setError(null)
    const userMsg = { role: 'user', content: text, timestamp: Date.now() }
    setMessages((prev) => [...prev, userMsg])

    try {
      const body = {
        message: text,
        session_id: sessionIdRef.current,
        context,
      }

      const res = await fetch(`${API_BASE}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        credentials: 'include',
      })

      if (!res.ok) throw new Error('Chat request failed')
      const data = await res.json()

      sessionIdRef.current = data.session_id
      const assistantMsg = {
        role: 'assistant',
        content: data.message,
        actions: data.actions || [],
        timestamp: Date.now(),
      }
      setMessages((prev) => [...prev, assistantMsg])

      return { actions: data.actions || [] }
    } catch (err) {
      setError(err.message)
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: 'Sorry, I had trouble connecting. Please try again.',
          isError: true,
          timestamp: Date.now(),
        },
      ])
      return { actions: [] }
    }
  }, [])

  const cancelStream = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    }
    setStreaming(false)
  }, [])

  const clearChat = useCallback(() => {
    setMessages([])
    setError(null)
    sessionIdRef.current = null
  }, [])

  return {
    messages,
    streaming,
    error,
    sendMessage,
    sendMessageSync,
    clearChat,
    cancelStream,
    sessionId: sessionIdRef.current,
  }
}
