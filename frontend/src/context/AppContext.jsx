import React, { createContext, useContext, useReducer, useCallback, useMemo } from 'react';
import { getLocalToday } from '../utils/dateUtils';

const AppContext = createContext(null);

const initialState = {
  view: 'slate',         // 'slate' | 'team' | 'lineup' | 'accuracy' | 'underdog'
  selectedTeam: null,     // { id, abbreviation, name, ... } or null
  selectedDate: getLocalToday(),  // 'YYYY-MM-DD' — defaults to today (local timezone)
  selectedSport: 'nba',  // 'nba' | 'cbb'
  showChat: false,
};

function appReducer(state, action) {
  switch (action.type) {
    case 'SET_VIEW':
      return { ...state, view: action.payload };
    case 'SET_SELECTED_TEAM':
      return { ...state, selectedTeam: action.payload };
    case 'SET_SELECTED_DATE':
      return { ...state, selectedDate: action.payload };
    case 'TOGGLE_CHAT':
      return { ...state, showChat: !state.showChat };
    case 'SET_SHOW_CHAT':
      return { ...state, showChat: action.payload };
    case 'SET_SELECTED_SPORT':
      // Clear team selection when switching sports (IDs differ)
      return { ...state, selectedSport: action.payload, selectedTeam: null };
    default:
      return state;
  }
}

export function AppProvider({ children }) {
  const [state, dispatch] = useReducer(appReducer, initialState);

  const setView = useCallback((view) => {
    dispatch({ type: 'SET_VIEW', payload: view });
  }, []);

  const setSelectedTeam = useCallback((team) => {
    dispatch({ type: 'SET_SELECTED_TEAM', payload: team });
  }, []);

  const setSelectedDate = useCallback((date) => {
    dispatch({ type: 'SET_SELECTED_DATE', payload: date });
  }, []);

  const toggleChat = useCallback(() => {
    dispatch({ type: 'TOGGLE_CHAT' });
  }, []);

  const setShowChat = useCallback((show) => {
    dispatch({ type: 'SET_SHOW_CHAT', payload: show });
  }, []);

  const setSelectedSport = useCallback((sport) => {
    dispatch({ type: 'SET_SELECTED_SPORT', payload: sport });
  }, []);

  const value = useMemo(() => ({
    ...state,
    setView,
    setSelectedTeam,
    setSelectedDate,
    toggleChat,
    setShowChat,
    setSelectedSport,
  }), [state, setView, setSelectedTeam, setSelectedDate, toggleChat, setShowChat, setSelectedSport]);

  return (
    <AppContext.Provider value={value}>
      {children}
    </AppContext.Provider>
  );
}

export function useAppContext() {
  const context = useContext(AppContext);
  if (!context) {
    throw new Error('useAppContext must be used within an AppProvider');
  }
  return context;
}

export default AppContext;
