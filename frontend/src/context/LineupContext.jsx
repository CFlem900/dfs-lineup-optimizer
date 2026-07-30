import React, { createContext, useContext, useReducer, useCallback, useRef } from 'react';

const LineupContext = createContext(null);

const initialState = {
  platform: 'dk',
  contestType: 'gpp',
  strategy: 'balanced',
  lineups: [],
  selectedLineupIdx: 0,
  pool: [],
  originalPool: [],
  lockedPlayers: [],    // array of player IDs
  excludedPlayers: [],  // array of player IDs
  projectionOverrides: {},
  numLineups: 5,
  poolLoaded: false,
  generating: false,
  analyzing: false,
};

function lineupReducer(state, action) {
  switch (action.type) {
    case 'SET_PLATFORM':
      return { ...state, platform: action.payload };
    case 'SET_CONTEST_TYPE':
      return { ...state, contestType: action.payload };
    case 'SET_STRATEGY':
      return { ...state, strategy: action.payload };
    case 'SET_LINEUPS':
      return { ...state, lineups: action.payload, selectedLineupIdx: 0 };
    case 'SELECT_LINEUP':
      return { ...state, selectedLineupIdx: action.payload };
    case 'SET_POOL':
      return { ...state, pool: action.payload, poolLoaded: true };
    case 'SET_ORIGINAL_POOL':
      return { ...state, originalPool: action.payload };
    case 'SET_NUM_LINEUPS':
      return { ...state, numLineups: action.payload };
    case 'SET_GENERATING':
      return { ...state, generating: action.payload };
    case 'SET_ANALYZING':
      return { ...state, analyzing: action.payload };
    case 'LOCK_PLAYER': {
      const id = action.payload;
      const locked = state.lockedPlayers.includes(id)
        ? state.lockedPlayers
        : [...state.lockedPlayers, id];
      const excluded = state.excludedPlayers.filter((eid) => eid !== id);
      return { ...state, lockedPlayers: locked, excludedPlayers: excluded };
    }
    case 'UNLOCK_PLAYER':
      return {
        ...state,
        lockedPlayers: state.lockedPlayers.filter((id) => id !== action.payload),
      };
    case 'EXCLUDE_PLAYER': {
      const id = action.payload;
      const excluded = state.excludedPlayers.includes(id)
        ? state.excludedPlayers
        : [...state.excludedPlayers, id];
      const locked = state.lockedPlayers.filter((lid) => lid !== id);
      return { ...state, excludedPlayers: excluded, lockedPlayers: locked };
    }
    case 'UNEXCLUDE_PLAYER':
      return {
        ...state,
        excludedPlayers: state.excludedPlayers.filter((id) => id !== action.payload),
      };
    case 'SET_PROJECTION_OVERRIDES':
      return { ...state, projectionOverrides: action.payload };
    case 'CLEAR_LOCKS':
      return { ...state, lockedPlayers: [], excludedPlayers: [] };
    case 'RESET':
      return { ...initialState };
    default:
      return state;
  }
}

export function LineupProvider({ children }) {
  const [state, dispatch] = useReducer(lineupReducer, initialState);

  // Convenience action creators
  const lockPlayer = useCallback((id) => dispatch({ type: 'LOCK_PLAYER', payload: id }), []);
  const unlockPlayer = useCallback((id) => dispatch({ type: 'UNLOCK_PLAYER', payload: id }), []);
  const excludePlayer = useCallback((id) => dispatch({ type: 'EXCLUDE_PLAYER', payload: id }), []);
  const unexcludePlayer = useCallback((id) => dispatch({ type: 'UNEXCLUDE_PLAYER', payload: id }), []);
  const setPlatform = useCallback((p) => dispatch({ type: 'SET_PLATFORM', payload: p }), []);
  const setContestType = useCallback((ct) => dispatch({ type: 'SET_CONTEST_TYPE', payload: ct }), []);
  const setStrategy = useCallback((s) => dispatch({ type: 'SET_STRATEGY', payload: s }), []);
  const setLineups = useCallback((l) => dispatch({ type: 'SET_LINEUPS', payload: l }), []);
  const selectLineup = useCallback((idx) => dispatch({ type: 'SELECT_LINEUP', payload: idx }), []);
  const setPool = useCallback((p) => dispatch({ type: 'SET_POOL', payload: p }), []);
  const setOriginalPool = useCallback((p) => dispatch({ type: 'SET_ORIGINAL_POOL', payload: p }), []);
  const setNumLineups = useCallback((n) => dispatch({ type: 'SET_NUM_LINEUPS', payload: n }), []);
  const setGenerating = useCallback((v) => dispatch({ type: 'SET_GENERATING', payload: v }), []);
  const setAnalyzing = useCallback((v) => dispatch({ type: 'SET_ANALYZING', payload: v }), []);
  const setProjectionOverrides = useCallback((o) => dispatch({ type: 'SET_PROJECTION_OVERRIDES', payload: o }), []);
  const clearLocks = useCallback(() => dispatch({ type: 'CLEAR_LOCKS' }), []);

  const getContext = useCallback(() => ({
    platform: state.platform,
    contestType: state.contestType,
    strategy: state.strategy,
    numLineups: state.numLineups,
    lockedPlayers: state.lockedPlayers,
    excludedPlayers: state.excludedPlayers,
    poolSize: state.pool.length,
    lineupCount: state.lineups.length,
  }), [state]);

  const value = {
    ...state,
    dispatch,
    lockPlayer,
    unlockPlayer,
    excludePlayer,
    unexcludePlayer,
    setPlatform,
    setContestType,
    setStrategy,
    setLineups,
    selectLineup,
    setPool,
    setOriginalPool,
    setNumLineups,
    setGenerating,
    setAnalyzing,
    setProjectionOverrides,
    clearLocks,
    getContext,
  };

  return (
    <LineupContext.Provider value={value}>
      {children}
    </LineupContext.Provider>
  );
}

export function useLineupContext() {
  const context = useContext(LineupContext);
  if (!context) {
    throw new Error('useLineupContext must be used within a LineupProvider');
  }
  return context;
}

export default LineupContext;
