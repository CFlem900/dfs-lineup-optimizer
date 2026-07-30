import React, { createContext, useContext, useState, useEffect, useCallback, useMemo } from 'react'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)
  const [providers, setProviders] = useState([])

  useEffect(() => {
    let cancelled = false
    async function checkAuth() {
      try {
        const provRes = await fetch('/api/auth/providers')
        const provData = provRes.ok ? await provRes.json() : { providers: [] }
        if (cancelled) return
        setProviders(provData.providers || [])

        // If no providers configured, auth is disabled — skip login wall
        if ((provData.providers || []).length === 0) {
          setUser({ id: 0, email: '', display_name: 'Local User', provider: 'none' })
          setLoading(false)
          return
        }

        const meRes = await fetch('/api/auth/me', { credentials: 'include' })
        if (!cancelled) {
          if (meRes.ok) {
            setUser(await meRes.json())
          } else {
            setUser(null)
          }
        }
      } catch {
        if (!cancelled) setUser(null)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    checkAuth()
    return () => { cancelled = true }
  }, [])

  const login = useCallback((provider) => {
    window.location.href = `/api/auth/login/${provider}`
  }, [])

  const logout = useCallback(async () => {
    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
        credentials: 'include',
      })
    } catch { /* ignore */ }
    setUser(null)
  }, [])

  const value = useMemo(() => ({
    user,
    loading,
    providers,
    isAuthenticated: !!user,
    login,
    logout,
  }), [user, loading, providers, login, logout])

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}

export default AuthContext
