/**
 * RotationEngine API service.
 *
 * Centralises all backend calls behind a thin async wrapper so that
 * components never need to know about URL construction or fetch
 * semantics.  Falls back to the Vite dev-proxy when VITE_API_URL is
 * not set.
 */

const API_BASE = import.meta.env.VITE_API_URL || '/api';

/** Fetch wrapper that always sends credentials (session cookie). */
function authFetch(url, options = {}) {
  return fetch(url, { ...options, credentials: 'include' });
}

/** Extract a useful error message from a non-ok response. */
async function extractError(res, fallback = 'Request failed') {
  try {
    const data = await res.json();
    return data.detail || data.message || data.error || fallback;
  } catch {
    return `HTTP ${res.status}: ${res.statusText || fallback}`;
  }
}

/**
 * Shared SSE stream parser — reads a ReadableStream line-by-line and
 * dispatches parsed `data:` events to a callback.
 *
 * @param {ReadableStreamDefaultReader} reader
 * @param {(parsed: object, raw: string) => void} onEvent
 */
async function _parseSSEStream(reader, onEvent) {
  const decoder = new TextDecoder();
  let buffer = '';
  let currentEvent = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      if (line.startsWith('event: ')) {
        currentEvent = line.slice(7).trim();
      } else if (line.startsWith('data: ')) {
        const raw = line.slice(6).trim();
        if (raw === '[DONE]' || raw === '{}') continue;
        try {
          const parsed = JSON.parse(raw);
          onEvent(parsed, raw, currentEvent);
        } catch {
          onEvent(null, raw, currentEvent);
        }
        currentEvent = null;
      } else if (line.trim() === '') {
        currentEvent = null;
      }
    }
  }
}

export const rotationAPI = {
  // ── Teams ────────────────────────────────────────────────────────
  async getAllTeams(sport = 'nba') {
    const params = sport !== 'nba' ? `?sport=${sport}` : '';
    const res = await authFetch(`${API_BASE}/teams${params}`);
    if (!res.ok) throw new Error('Failed to fetch teams');
    return res.json();
  },

  async getTeamsByConference(sport = 'cbb') {
    const params = new URLSearchParams({ sport });
    const res = await authFetch(`${API_BASE}/teams/conferences?${params}`);
    if (!res.ok) throw new Error('Failed to fetch conferences');
    return res.json();
  },

  // ── Rotation ─────────────────────────────────────────────────────
  async getTeamRotation(teamId, { gameDate, draftGroupId, sport = 'nba' } = {}) {
    const params = new URLSearchParams();
    if (gameDate) params.set('game_date', gameDate);
    if (draftGroupId) params.set('draft_group_id', String(draftGroupId));
    if (sport !== 'nba') params.set('sport', sport);
    const qs = params.toString();
    const res = await authFetch(`${API_BASE}/teams/${teamId}/rotation${qs ? `?${qs}` : ''}`);
    if (!res.ok) throw new Error('Failed to fetch rotation');
    return res.json();
  },

  // ── Injuries ─────────────────────────────────────────────────────
  async getTeamInjuries(teamId, { sport = 'nba' } = {}) {
    const params = sport !== 'nba' ? `?sport=${sport}` : '';
    const res = await authFetch(`${API_BASE}/teams/${teamId}/injuries${params}`);
    if (!res.ok) throw new Error('Failed to fetch injuries');
    return res.json();
  },

  async getAllInjuries() {
    const res = await authFetch(`${API_BASE}/injuries`);
    if (!res.ok) throw new Error('Failed to fetch injuries');
    return res.json();
  },

  // ── Scoreboard & Games ───────────────────────────────────────────
  async getScoreboard(gameDate, sport = 'nba') {
    const params = new URLSearchParams();
    if (gameDate) params.set('game_date', gameDate);
    if (sport !== 'nba') params.set('sport', sport);
    const qs = params.toString();
    const res = await authFetch(`${API_BASE}/scoreboard${qs ? `?${qs}` : ''}`);
    if (!res.ok) throw new Error('Failed to fetch scoreboard');
    return res.json();
  },

  async getTeamGameToday(teamId, gameDate, sport = 'nba') {
    const params = new URLSearchParams();
    if (gameDate) params.set('game_date', gameDate);
    if (sport !== 'nba') params.set('sport', sport);
    const qs = params.toString();
    const res = await authFetch(`${API_BASE}/teams/${teamId}/game-today${qs ? `?${qs}` : ''}`);
    if (!res.ok) throw new Error('Failed to fetch game info');
    return res.json();
  },

  // ── Simulation ───────────────────────────────────────────────────
  async simulateGame(gameId, { gameDate, numSimulations = 10000, overUnderLine, sport = 'nba' } = {}) {
    const params = new URLSearchParams({ num_simulations: String(numSimulations) });
    if (gameDate) params.set('game_date', gameDate);
    if (overUnderLine != null) params.set('over_under_line', String(overUnderLine));
    if (sport !== 'nba') params.set('sport', sport);
    const res = await authFetch(`${API_BASE}/games/${gameId}/simulate?${params}`);
    if (!res.ok) throw new Error('Simulation failed');
    return res.json();
  },

  // ── Coaches ──────────────────────────────────────────────────────
  async getCoaches() {
    const res = await authFetch(`${API_BASE}/coaches`);
    if (!res.ok) throw new Error('Failed to fetch coaches');
    return res.json();
  },

  async getCoachForTeam(teamId) {
    const res = await authFetch(`${API_BASE}/coaches/${teamId}`);
    if (!res.ok) throw new Error('Failed to fetch coach');
    return res.json();
  },

  // ── Player Projection ───────────────────────────────────────────
  async getPlayerProjection(teamId, playerId, sport = 'nba') {
    const params = sport !== 'nba' ? `?sport=${sport}` : '';
    const res = await authFetch(`${API_BASE}/projection/${teamId}/player/${playerId}${params}`);
    if (!res.ok) throw new Error('Failed to fetch player projection');
    return res.json();
  },

  // ── News ─────────────────────────────────────────────────────────
  async getNews({ teamIds, playerIds, limit = 50, sport = 'nba' } = {}) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (teamIds && teamIds.length) {
      teamIds.forEach((id) => params.append('team_id', String(id)));
    }
    if (playerIds && playerIds.length) {
      playerIds.forEach((id) => params.append('player_id', String(id)));
    }
    if (sport !== 'nba') params.set('sport', sport);
    const res = await authFetch(`${API_BASE}/news?${params}`);
    if (!res.ok) throw new Error('Failed to fetch news');
    return res.json();
  },

  // ── Expert Signals ───────────────────────────────────────────────
  async getExpertSignals({ teamAbbr, playerNames, limit = 30, sport = 'nba' } = {}) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (teamAbbr) params.set('team_abbr', teamAbbr);
    if (playerNames && playerNames.length) {
      params.set('player_names', playerNames.join(','));
    }
    if (sport !== 'nba') params.set('sport', sport);
    const res = await authFetch(`${API_BASE}/expert-signals?${params}`);
    if (!res.ok) throw new Error('Failed to fetch expert signals');
    return res.json();
  },

  // ── Accuracy (projected vs actual) ──────────────────────────────
  async getAccuracy({ teamId, startDate, endDate, limit = 50, sport = 'nba' } = {}) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (teamId) params.set('team_id', String(teamId));
    if (startDate) params.set('start_date', startDate);
    if (endDate) params.set('end_date', endDate);
    if (sport !== 'nba') params.set('sport', sport);
    const res = await authFetch(`${API_BASE}/accuracy?${params}`);
    if (!res.ok) throw new Error('Failed to fetch accuracy data');
    return res.json();
  },

  // ── Lineup Optimizer ──────────────────────────────────────────
  async getPlayerPool({ platform = 'dk', draftGroupId, gameDate, sport = 'nba' } = {}) {
    const params = new URLSearchParams({ platform });
    if (draftGroupId) params.set('draft_group_id', String(draftGroupId));
    if (gameDate) params.set('game_date', gameDate);
    if (sport !== 'nba') params.set('sport', sport);
    const res = await authFetch(`${API_BASE}/player-pool?${params}`);
    if (!res.ok) throw new Error('Failed to fetch player pool');
    return res.json();
  },

  /**
   * Stream player pool build with real-time progress via SSE.
   *
   * @param {object} opts - { platform, draftGroupId, gameDate }
   * @param {function} onProgress - Called with { step, completed, total }
   * @returns {Promise<{ players, count }>} Resolves when pool is complete
   */
  streamPlayerPool({ platform = 'dk', draftGroupId, gameDate, sport = 'nba' } = {}, onProgress) {
    const params = new URLSearchParams({ platform });
    if (draftGroupId) params.set('draft_group_id', String(draftGroupId));
    if (gameDate) params.set('game_date', gameDate);
    if (sport !== 'nba') params.set('sport', sport);

    // CBB pools take longer due to per-team CBBpy scraping (~7s each)
    const timeoutMs = sport === 'cbb' ? 420_000 : 240_000
    const timeoutLabel = sport === 'cbb' ? '7 minutes' : '4 minutes'

    return new Promise((resolve, reject) => {
      let settled = false
      const source = new EventSource(`${API_BASE}/player-pool/stream?${params}`, { withCredentials: true });

      // Safety timeout — cold pool builds can take several minutes
      const timeout = setTimeout(() => {
        if (!settled) {
          settled = true
          source.close()
          reject(new Error(`Pool build timed out after ${timeoutLabel}`))
        }
      }, timeoutMs)

      source.addEventListener('progress', (e) => {
        try {
          const data = JSON.parse(e.data);
          if (onProgress) onProgress(data);
        } catch (err) { /* ignore parse errors */ }
      });

      source.addEventListener('done', (e) => {
        if (settled) return
        settled = true
        clearTimeout(timeout)
        source.close();
        try {
          resolve(JSON.parse(e.data));
        } catch (err) {
          reject(new Error('Failed to parse pool data'));
        }
      });

      // Server-sent error event (named "error" from backend)
      source.addEventListener('error', (e) => {
        if (settled) return
        settled = true
        clearTimeout(timeout)
        source.close();
        try {
          const data = JSON.parse(e.data);
          reject(new Error(data.detail || 'Stream failed'));
        } catch {
          // Native EventSource error (connection failed, etc.)
          reject(new Error('Player pool stream failed'));
        }
      });

      // Native EventSource connection error (fires before named events)
      source.onerror = () => {
        if (settled) return
        settled = true
        clearTimeout(timeout)
        source.close();
        reject(new Error('SSE connection failed'));
      };
    });
  },

  /**
   * Clear server-side pool cache for a specific slate.
   * Called when user clicks "Refresh Pool" to force fresh data.
   */
  async getInjuryHash() {
    try {
      const res = await authFetch(`${API_BASE}/player-pool/injury-hash`)
      if (!res.ok) return null
      const data = await res.json()
      return data.injury_hash || null
    } catch {
      return null
    }
  },

  async clearPoolCache({ platform = 'dk', draftGroupId, gameDate, sport = 'nba' } = {}) {
    const params = new URLSearchParams({ platform });
    if (draftGroupId) params.set('draft_group_id', String(draftGroupId));
    if (gameDate) params.set('game_date', gameDate);
    if (sport !== 'nba') params.set('sport', sport);
    const res = await authFetch(`${API_BASE}/player-pool/clear-cache?${params}`, { method: 'POST' });
    if (!res.ok) throw new Error('Failed to clear pool cache');
    return res.json();
  },

  async optimizeLineup({ platform = 'dk', draftGroupId, gameDate, lockedPlayers = [], excludedPlayers = [], projectionOverrides = null, seed = null, mode = 'classic', gameId = null, sport = 'nba', contestType = 'gpp' } = {}) {
    const body = {
      platform,
      sport,
      draft_group_id: draftGroupId,
      game_date: gameDate || null,
      locked_players: lockedPlayers,
      excluded_players: excludedPlayers,
      mode,
      contest_type: contestType,
    };
    if (gameId) {
      body.game_id = gameId;
    }
    if (projectionOverrides && Object.keys(projectionOverrides).length > 0) {
      body.projection_overrides = projectionOverrides;
    }
    if (seed !== null) {
      body.seed = seed;
    }
    const res = await authFetch(`${API_BASE}/optimize-lineup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      throw new Error(await extractError(res, 'Lineup optimization failed'));
    }
    return res.json();
  },

  // ── Pool Preloading ────────────────────────────────────────────
  async preloadPool({ platform = 'dk', draftGroupId, gameDate, sport = 'nba' } = {}) {
    const params = new URLSearchParams({
      platform,
      draft_group_id: draftGroupId,
    });
    if (gameDate) params.append('game_date', gameDate);
    if (sport !== 'nba') params.set('sport', sport);

    const res = await authFetch(`${API_BASE}/preload-pool?${params}`, {
      method: 'POST',
    });
    if (!res.ok) {
      throw new Error(await extractError(res, 'Pool preload failed'));
    }
    return res.json();
  },

  // ── Multi-Lineup Generation ─────────────────────────────────────
  async generateLineups({
    platform = 'dk', draftGroupId, gameDate,
    lockedPlayers = [], excludedPlayers = [],
    numLineups = 1, strategy = 'max_projection', maxOverlap = 6,
    contestType = 'gpp', projectionOverrides = null, seed = null,
    mode = 'classic', gameId = null, maxExposure = null,
    recentWeight = null, sport = 'nba', optimalityThreshold = null,
    minimumRelaxationFloor = null, isLateSwap = false,
    enableStacking, salaryFloorPct, playerMaxExposure,
    // Dynamic stacking overrides (Prompt 5.3)
    primaryStackSize = null, secondaryStackSize = null, requireBringBack = null,
  } = {}) {
    const body = {
      platform,
      sport,
      draft_group_id: draftGroupId,
      game_date: gameDate || null,
      locked_players: lockedPlayers,
      excluded_players: excludedPlayers,
      num_lineups: numLineups,
      strategy,
      max_overlap: maxOverlap,
      contest_type: contestType,
      mode,
    };
    if (gameId) {
      body.game_id = gameId;
    }
    if (maxExposure !== null && maxExposure !== undefined) {
      body.max_exposure = maxExposure;
    }
    if (recentWeight !== null && recentWeight !== undefined) {
      body.recent_weight = recentWeight;
    }
    if (projectionOverrides && Object.keys(projectionOverrides).length > 0) {
      body.projection_overrides = projectionOverrides;
    }
    if (seed !== null) {
      body.seed = seed;
    }
    if (optimalityThreshold !== null && optimalityThreshold !== undefined) {
      body.optimality_threshold = optimalityThreshold;
    }
    if (minimumRelaxationFloor !== null && minimumRelaxationFloor !== undefined) {
      body.minimum_relaxation_floor = minimumRelaxationFloor;
    }
    if (isLateSwap) {
      body.is_late_swap = true;
    }
    if (enableStacking !== undefined && enableStacking !== null) {
      body.enable_stacking = enableStacking;
    }
    if (salaryFloorPct !== undefined && salaryFloorPct !== null) {
      body.salary_floor_pct = salaryFloorPct;
    }
    if (playerMaxExposure && Object.keys(playerMaxExposure).length > 0) {
      body.player_max_exposure = playerMaxExposure;
    }
    // Dynamic stacking overrides (Prompt 5.3) — only include keys the
    // user actually set so the backend falls back to SportConfig
    // defaults for the rest.
    if (primaryStackSize !== null && primaryStackSize !== undefined) {
      body.primary_stack_size = primaryStackSize;
    }
    if (secondaryStackSize !== null && secondaryStackSize !== undefined) {
      body.secondary_stack_size = secondaryStackSize;
    }
    if (requireBringBack !== null && requireBringBack !== undefined) {
      body.require_bring_back = requireBringBack;
    }
    // Multi-lineup generation can take 60-90s for large slates.
    // Use AbortController to prevent indefinite hangs.
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 600000); // 10 min
    try {
      const res = await authFetch(`${API_BASE}/generate-lineups`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok) {
        throw new Error(await extractError(res, 'Multi-lineup generation failed'));
      }
      return res.json();
    } catch (err) {
      if (err.name === 'AbortError') {
        throw new Error(
          `Multi-lineup generation timed out after 600s. ` +
          `Try fewer lineups (${numLineups} requested) or a smaller slate.`
        );
      }
      throw err;
    } finally {
      clearTimeout(timeoutId);
    }
  },

  // ── Simulate & Filter Pipeline ──────────────────────────────────
  async simFilterLineups({
    platform = 'dk', draftGroupId, gameDate,
    lockedPlayers = [], excludedPlayers = [],
    numSimulations = 1000, numLineups = 20,
    solverMode = 'greedy', contestType = 'gpp',
    projectionOverrides = null, seed = null,
    mode = 'classic', gameId = null, sport = 'nba',
  } = {}) {
    const body = {
      platform,
      sport,
      draft_group_id: draftGroupId,
      game_date: gameDate || null,
      locked_players: lockedPlayers,
      excluded_players: excludedPlayers,
      num_simulations: numSimulations,
      num_lineups: numLineups,
      solver_mode: solverMode,
      contest_type: contestType,
      mode,
    };
    if (gameId) body.game_id = gameId;
    if (projectionOverrides && Object.keys(projectionOverrides).length > 0) {
      body.projection_overrides = projectionOverrides;
    }
    if (seed !== null) body.seed = seed;
    const res = await authFetch(`${API_BASE}/sim-filter-lineups`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      throw new Error(await extractError(res, 'Sim-filter generation failed'));
    }
    return res.json();
  },

  // ── Lineup Analysis ─────────────────────────────────────────────
  async analyzeLineups({ platform = 'dk', draftGroupId, gameDate, lineups = [], sport = 'nba' } = {}) {
    const res = await authFetch(`${API_BASE}/analyze-lineups`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        platform,
        sport,
        draft_group_id: draftGroupId,
        game_date: gameDate || null,
        lineups,
      }),
    });
    if (!res.ok) {
      throw new Error(await extractError(res, 'Lineup analysis failed'));
    }
    return res.json();
  },

  // ── Lineup Refinement ─────────────────────────────────────────
  async refineLineups({
    platform = 'dk', draftGroupId, gameDate, lineups = [],
    maxIterations = 3, targetGrade = null, sport = 'nba',
  } = {}) {
    const res = await authFetch(`${API_BASE}/refine-lineups`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        platform,
        sport,
        draft_group_id: draftGroupId,
        game_date: gameDate || null,
        lineups,
        max_iterations: maxIterations,
        target_grade: targetGrade,
      }),
    });
    if (!res.ok) {
      throw new Error(await extractError(res, 'Lineup refinement failed'));
    }
    return res.json();
  },

  // ── AI Chat ─────────────────────────────────────────────────
  async sendChat({ message, sessionId, context } = {}) {
    const res = await authFetch(`${API_BASE}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, session_id: sessionId, context }),
    });
    if (!res.ok) throw new Error('Chat request failed');
    return res.json();
  },

  /**
   * Stream chat response via POST with streaming body.
   *
   * @param {object} opts - { message, sessionId, context }
   * @param {function} onChunk - Called with each text chunk
   * @returns {Promise<{message, actions, session_id}>}
   */
  async streamChat({ message, sessionId, context } = {}, onChunk) {
    const res = await authFetch(`${API_BASE}/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, session_id: sessionId, context }),
    });
    if (!res.ok) throw new Error('Chat stream failed');

    let fullText = '';
    let earlyReturn = null;

    await _parseSSEStream(res.body.getReader(), (parsed, raw) => {
      if (parsed) {
        if (parsed.chunk && onChunk) {
          fullText += parsed.chunk;
          onChunk(parsed.chunk, fullText);
        }
        if (parsed.actions) {
          earlyReturn = { message: fullText, actions: parsed.actions, session_id: parsed.session_id };
        }
      } else if (onChunk) {
        fullText += raw;
        onChunk(raw, fullText);
      }
    });

    return earlyReturn || { message: fullText, actions: [], session_id: sessionId };
  },

  // ── AI Narrative Analysis (SSE streaming) ──────────────────
  /**
   * Stream narrative analysis for lineups via SSE.
   *
   * @param {object} opts - { platform, draftGroupId, gameDate, lineups }
   * @param {function} onChunk - Called with each text chunk
   * @returns {Promise<string>} Full narrative text
   */
  async streamNarrative({ platform = 'dk', draftGroupId, gameDate, lineups = [], sport = 'nba' } = {}, onChunk) {
    const res = await authFetch(`${API_BASE}/analyze-lineups/narrative`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        platform,
        sport,
        draft_group_id: draftGroupId,
        game_date: gameDate || null,
        lineups,
      }),
    });
    if (!res.ok) throw new Error('Narrative stream failed');

    let fullText = '';

    await _parseSSEStream(res.body.getReader(), (parsed, raw) => {
      if (parsed && parsed.text) {
        fullText += parsed.text;
        if (onChunk) onChunk(parsed.text, fullText);
      } else if (!parsed) {
        fullText += raw;
        if (onChunk) onChunk(raw, fullText);
      }
    });

    return fullText;
  },

  // ── Backtest ───────────────────────────────────────────────
  async triggerBacktestIngest({ gameDate } = {}) {
    const params = gameDate ? `?game_date=${gameDate}` : '';
    const res = await authFetch(`${API_BASE}/backtest/ingest${params}`, { method: 'POST' });
    if (!res.ok) throw new Error('Backtest ingestion failed');
    return res.json();
  },

  async getBacktestAnalysis({ days = 30 } = {}) {
    const res = await authFetch(`${API_BASE}/backtest/analysis?days=${days}`);
    if (!res.ok) throw new Error('Backtest analysis failed');
    return res.json();
  },

  // ── AI Status ──────────────────────────────────────────────
  async getAIStatus() {
    const res = await authFetch(`${API_BASE}/ai/status`);
    if (!res.ok) throw new Error('Failed to fetch AI status');
    return res.json();
  },

  // ── Tournament Analysis ───────────────────────────────────
  async importTournamentCSV({ file, contestDate, contestName, contestType = 'gpp' } = {}) {
    const formData = new FormData();
    formData.append('file', file);
    const params = new URLSearchParams({ contest_date: contestDate, contest_type: contestType });
    if (contestName) params.set('contest_name', contestName);
    const res = await authFetch(`${API_BASE}/tournament/import?${params}`, {
      method: 'POST',
      body: formData,
    });
    if (!res.ok) {
      throw new Error(await extractError(res, 'Tournament import failed'));
    }
    return res.json();
  },

  /**
   * Batch import multiple tournament CSVs in a single request.
   * Auto-detects contest dates from filenames (YYYY-MM-DD pattern).
   */
  async importTournamentBatch({ files, contestType = 'gpp' } = {}) {
    const formData = new FormData();
    for (const file of files) {
      formData.append('files', file);
    }
    const params = new URLSearchParams({ contest_type: contestType });
    const res = await authFetch(`${API_BASE}/tournament/import-batch?${params}`, {
      method: 'POST',
      body: formData,
    });
    if (!res.ok) {
      throw new Error(await extractError(res, 'Batch tournament import failed'));
    }
    return res.json();
  },

  async getTournamentAnalysis() {
    const res = await authFetch(`${API_BASE}/tournament/analysis`);
    if (!res.ok) throw new Error('Tournament analysis failed');
    return res.json();
  },

  async getTournamentCalibrations() {
    const res = await authFetch(`${API_BASE}/tournament/calibrations`);
    if (!res.ok) throw new Error('Failed to fetch calibrations');
    return res.json();
  },

  async resetTournamentCalibrations({ source = null } = {}) {
    const params = source ? `?source=${source}` : '';
    const res = await authFetch(`${API_BASE}/tournament/calibrations/reset${params}`, { method: 'POST' });
    if (!res.ok) throw new Error('Failed to reset calibrations');
    return res.json();
  },

  // ── Projection Accuracy ──────────────────────────────────────
  async getAccuracySummary({ days = 30, sport = 'nba' } = {}) {
    const params = new URLSearchParams({ days: String(days) });
    if (sport !== 'nba') params.set('sport', sport);
    const res = await authFetch(`${API_BASE}/accuracy/summary?${params}`);
    if (!res.ok) throw new Error('Failed to fetch accuracy summary');
    return res.json();
  },

  async getAccuracyTimeline({ days = 90, sport = 'nba' } = {}) {
    const params = new URLSearchParams({ days: String(days) });
    if (sport !== 'nba') params.set('sport', sport);
    const res = await authFetch(`${API_BASE}/accuracy/timeline?${params}`);
    if (!res.ok) throw new Error('Failed to fetch accuracy timeline');
    return res.json();
  },

  async getAccuracyByPosition({ days = 30, sport = 'nba' } = {}) {
    const params = new URLSearchParams({ days: String(days) });
    if (sport !== 'nba') params.set('sport', sport);
    const res = await authFetch(`${API_BASE}/accuracy/by-position?${params}`);
    if (!res.ok) throw new Error('Failed to fetch accuracy by position');
    return res.json();
  },

  async getAccuracyBySalaryTier({ days = 30, sport = 'nba' } = {}) {
    const params = new URLSearchParams({ days: String(days) });
    if (sport !== 'nba') params.set('sport', sport);
    const res = await authFetch(`${API_BASE}/accuracy/by-salary-tier?${params}`);
    if (!res.ok) throw new Error('Failed to fetch accuracy by salary tier');
    return res.json();
  },

  async getPlayerAccuracy({ playerId, days = 90, sport = 'nba' } = {}) {
    const params = new URLSearchParams({ days: String(days) });
    if (sport !== 'nba') params.set('sport', sport);
    const res = await authFetch(`${API_BASE}/accuracy/player/${playerId}?${params}`);
    if (!res.ok) throw new Error('Failed to fetch player accuracy');
    return res.json();
  },

  // ── Late-Swap Monitor ────────────────────────────────────────
  async getLateSwapMonitor({ gameDate } = {}) {
    const params = gameDate ? `?game_date=${gameDate}` : '';
    const res = await authFetch(`${API_BASE}/late-swap/monitor${params}`);
    if (!res.ok) throw new Error('Failed to fetch late-swap updates');
    return res.json();
  },

  // ── Live Entry Import ───────────────────────────────────────
  async importEntries({ draftGroupId, file, sport = 'nba' }) {
    const formData = new FormData();
    formData.append('file', file);
    const params = new URLSearchParams({ sport });
    const res = await authFetch(
      `${API_BASE}/slates/${draftGroupId}/import-entries?${params}`,
      { method: 'POST', body: formData },
    );
    if (!res.ok) throw new Error(await extractError(res, 'Entry import failed'));
    return res.json();
  },

  /**
   * Parse a DraftKings entries CSV without persisting — returns
   * contests grouped by ID with roster slot → player name mapping.
   */
  async parseEntriesCSV(file) {
    const formData = new FormData()
    formData.append('file', file)
    const res = await authFetch(`${API_BASE}/slates/parse-entries-csv`, {
      method: 'POST',
      body: formData,
    })
    if (!res.ok) throw new Error(await extractError(res, 'Failed to parse entries CSV'))
    return res.json()
  },

  /**
   * Fill a DK entries CSV with generated lineups and download the result.
   *
   * @param {File} file - The original DK entries CSV file
   * @param {Array<Array<{dk_player_id: number, display_name: string}>>} lineups
   *        Lineups in roster slot order
   * @param {boolean} allowDuplicates - Allow same lineup across entries
   * @returns {Promise<{blob: Blob, meta: object}>} Filled CSV blob + metadata
   */
  async fillEntriesCSV({ file, lineups, allowDuplicates = false }) {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('lineups', JSON.stringify(lineups))
    formData.append('allow_duplicates', String(allowDuplicates))
    const res = await authFetch(`${API_BASE}/slates/fill-entries-csv`, {
      method: 'POST',
      body: formData,
    })
    if (!res.ok) throw new Error(await extractError(res, 'Failed to fill entries CSV'))
    const blob = await res.blob()
    return {
      blob,
      meta: {
        entriesFilled: parseInt(res.headers.get('X-Entries-Filled') || '0', 10),
        lineupsUsed: parseInt(res.headers.get('X-Lineups-Used') || '0', 10),
        warnings: JSON.parse(res.headers.get('X-Warnings') || '[]'),
        contestSummary: JSON.parse(res.headers.get('X-Contest-Summary') || '{}'),
      },
    }
  },

  async getImportedEntries({ draftGroupId, sport = 'nba' }) {
    const params = new URLSearchParams({ sport });
    const res = await authFetch(
      `${API_BASE}/slates/${draftGroupId}/entries?${params}`,
    );
    if (!res.ok) throw new Error('Failed to fetch imported entries');
    return res.json();
  },

  async deleteImportedEntries({ draftGroupId, sport = 'nba' }) {
    const params = new URLSearchParams({ sport });
    const res = await authFetch(
      `${API_BASE}/slates/${draftGroupId}/entries?${params}`,
      { method: 'DELETE' },
    );
    if (!res.ok) throw new Error('Failed to delete imported entries');
    return res.json();
  },

  async lateSwapEntry({ draftGroupId, entryId, sport = 'nba', gameDate = null }) {
    const params = new URLSearchParams({ sport });
    if (gameDate) params.set('game_date', gameDate);
    const res = await authFetch(
      `${API_BASE}/slates/${draftGroupId}/entries/${entryId}/late-swap?${params}`,
      { method: 'POST' },
    );
    if (!res.ok) throw new Error(await extractError(res, 'Late-swap failed'));
    return res.json();
  },

  async lateSwapAllEntries({ draftGroupId, sport = 'nba', gameDate = null }) {
    const params = new URLSearchParams({ sport });
    if (gameDate) params.set('game_date', gameDate);
    const res = await authFetch(
      `${API_BASE}/slates/${draftGroupId}/entries/late-swap-all?${params}`,
      { method: 'POST' },
    );
    if (!res.ok) throw new Error(await extractError(res, 'Late-swap-all failed'));
    return res.json();
  },

  async exportEntriesCsv({ draftGroupId, sport = 'nba' }) {
    const params = new URLSearchParams({ sport });
    const res = await authFetch(
      `${API_BASE}/slates/${draftGroupId}/entries/export-csv?${params}`,
    );
    if (!res.ok) throw new Error(await extractError(res, 'Export entries CSV failed'));
    return res.blob();
  },

  // ── Contrarian / Fade List ───────────────────────────────────
  async getFadeList({ platform = 'dk', draftGroupId, gameDate } = {}) {
    const params = new URLSearchParams({ platform });
    if (draftGroupId) params.set('draft_group_id', String(draftGroupId));
    if (gameDate) params.set('game_date', gameDate);
    const res = await authFetch(`${API_BASE}/fade-list?${params}`);
    if (!res.ok) throw new Error('Failed to fetch fade list');
    return res.json();
  },

  // ── AI Usage ─────────────────────────────────────────────────
  async getAIUsage({ days = 7 } = {}) {
    const res = await authFetch(`${API_BASE}/admin/ai-usage?days=${days}`);
    if (!res.ok) throw new Error('Failed to fetch AI usage');
    return res.json();
  },

  // ── Coach Learned Profiles ───────────────────────────────────
  async getCoachLearned(teamId) {
    const res = await authFetch(`${API_BASE}/coaches/${teamId}/learned`);
    if (!res.ok) throw new Error('Failed to fetch coach learned data');
    return res.json();
  },

  // ── Calibration History ──────────────────────────────────────
  async getCalibrationHistory({ key } = {}) {
    const params = key ? `?key=${key}` : '';
    const res = await authFetch(`${API_BASE}/tournament/calibrations/history${params}`);
    if (!res.ok) throw new Error('Failed to fetch calibration history');
    return res.json();
  },

  async rollbackCalibration(key) {
    const res = await authFetch(`${API_BASE}/tournament/calibrations/${key}/rollback`, { method: 'POST' });
    if (!res.ok) throw new Error('Failed to rollback calibration');
    return res.json();
  },

  // ── Ownership Simulation ──────────────────────────────────────
  async simulateOwnership({ lineup, fieldSize = 1000, numSimulations = 500 } = {}) {
    const res = await authFetch(`${API_BASE}/ownership/simulate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        lineup,
        field_size: fieldSize,
        num_simulations: numSimulations,
      }),
    });
    if (!res.ok) throw new Error('Failed to run ownership simulation');
    return res.json();
  },

  // ── Underdog Fantasy Pick'em ────────────────────────────────
  async getUnderdogLines({ gameDate, sport = 'nba' } = {}) {
    const params = new URLSearchParams();
    if (gameDate) params.set('game_date', gameDate);
    if (sport !== 'nba') params.set('sport', sport);
    const qs = params.toString() ? `?${params}` : '';
    const res = await authFetch(`${API_BASE}/underdog/pickem/lines${qs}`);
    if (!res.ok) throw new Error('Failed to fetch Underdog lines');
    return res.json();
  },

  async getUnderdogEdges({ gameDate, sport = 'nba', stat, minEdge } = {}) {
    const params = new URLSearchParams();
    if (gameDate) params.set('game_date', gameDate);
    if (sport !== 'nba') params.set('sport', sport);
    if (stat) params.set('stat', stat);
    if (minEdge) params.set('min_edge', minEdge);
    const qs = params.toString() ? `?${params}` : '';
    const res = await authFetch(`${API_BASE}/underdog/pickem/edges${qs}`);
    if (!res.ok) throw new Error('Failed to fetch Underdog edges');
    return res.json();
  },

  async getUnderdogPlayerEdges(playerName, { gameDate, sport = 'nba' } = {}) {
    const params = new URLSearchParams();
    if (gameDate) params.set('game_date', gameDate);
    if (sport !== 'nba') params.set('sport', sport);
    const qs = params.toString() ? `?${params}` : '';
    const res = await authFetch(`${API_BASE}/underdog/pickem/player/${encodeURIComponent(playerName)}${qs}`);
    if (!res.ok) throw new Error('Failed to fetch player edges');
    return res.json();
  },

  async buildPickemEntry({ numPicks = 5, strategy = 'max_edge', entryType = 'flex', gameDate, sport = 'nba' } = {}) {
    const res = await authFetch(`${API_BASE}/underdog/pickem/build-entry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        num_picks: numPicks,
        strategy,
        entry_type: entryType,
        game_date: gameDate,
        sport,
      }),
    });
    if (!res.ok) throw new Error('Failed to build pick\'em entry');
    return res.json();
  },

  async getPickemCorrelations({ gameDate, sport = 'nba' } = {}) {
    const params = new URLSearchParams();
    if (gameDate) params.set('game_date', gameDate);
    if (sport !== 'nba') params.set('sport', sport);
    const qs = params.toString() ? `?${params}` : '';
    const res = await authFetch(`${API_BASE}/underdog/pickem/correlations${qs}`);
    if (!res.ok) throw new Error('Failed to fetch correlations');
    return res.json();
  },

  // ── DraftKings Entry Automation ─────────────────────────────

  /** Check DK authentication status. */
  async dkAuthCheck() {
    const res = await authFetch(`${API_BASE}/dk-entries/auth-check`);
    if (!res.ok) throw new Error(await extractError(res, 'Failed to check DK auth'));
    return res.json();
  },

  /**
   * Download DK entries template via SSE stream.
   * @param {object} opts - { draftGroupId, sport }
   * @param {function} onProgress - Called with { step, detail }
   * @returns {Promise<object>} Parsed DKEntriesTemplate
   */
  dkDownloadTemplate({ draftGroupId, sport = 'cbb' } = {}, onProgress) {
    const params = new URLSearchParams({
      draft_group_id: String(draftGroupId),
      sport,
    });

    return new Promise((resolve, reject) => {
      let settled = false;
      const source = new EventSource(
        `${API_BASE}/dk-entries/download-template?${params}`,
        { withCredentials: true },
      );

      const timeout = setTimeout(() => {
        if (!settled) {
          settled = true;
          source.close();
          reject(new Error('DK template download timed out (2 min)'));
        }
      }, 120_000);

      source.addEventListener('progress', (e) => {
        try {
          const data = JSON.parse(e.data);
          if (onProgress) onProgress(data);
        } catch { /* ignore parse errors */ }
      });

      source.addEventListener('done', (e) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        source.close();
        try {
          resolve(JSON.parse(e.data));
        } catch {
          reject(new Error('Failed to parse template data'));
        }
      });

      source.addEventListener('error', (e) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        source.close();
        // SSE 'error' events can be native (no data) or server-sent (with data)
        if (e.data) {
          try {
            const data = JSON.parse(e.data);
            reject(new Error(data.error || 'Template download failed'));
          } catch {
            reject(new Error('Template download failed'));
          }
        } else {
          reject(new Error('SSE connection failed'));
        }
      });

      // Note: native connection errors are handled by the 'error' event
      // listener above (when e.data is absent).
    });
  },

  /** Fill a DK entries template with lineup IDs.
   * @param {object} args
   * @param {object} args.template
   * @param {number[][]} args.lineupPlayerIds - dk_player_ids per lineup
   * @param {Array<{projection:number,floor:number,ceiling:number}>} [args.lineupMeta]
   *   Optional per-lineup scores (parallel to lineupPlayerIds). Required for
   *   the smart selector to tier lineups against contest type.
   * @param {{mode?:'auto'|'round_robin', max_exposure_pct?:number, dedupe_per_contest?:boolean}} [args.selection]
   */
  async dkFillEntries({
    template,
    lineupPlayerIds,
    lineupMeta,
    selection,
  } = {}) {
    const res = await authFetch(`${API_BASE}/dk-entries/fill`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        template,
        lineup_player_ids: lineupPlayerIds,
        ...(lineupMeta ? { lineup_meta: lineupMeta } : {}),
        ...(selection ? { selection } : {}),
      }),
    });
    if (!res.ok) throw new Error(await extractError(res, 'Fill entries failed'));
    return res.json();
  },

  /**
   * Upload filled entries CSV to DK via SSE stream.
   * @param {string} filledCsv - The filled CSV content
   * @param {function} onProgress - Called with { step, detail }
   * @returns {Promise<object>} DKUploadResult
   */
  async dkUploadEntries(filledCsv, onProgress) {
    const res = await authFetch(`${API_BASE}/dk-entries/upload`, {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain' },
      body: filledCsv,
    });
    if (!res.ok) {
      throw new Error(await extractError(res, 'Upload failed'));
    }
    let result = null;
    await _parseSSEStream(res.body.getReader(), (parsed, _raw, eventName) => {
      if (!parsed) return;
      if (eventName === 'error' || parsed.error) {
        throw new Error(parsed?.error || 'Upload failed');
      }
      if (eventName === 'progress' || parsed.step) {
        if (onProgress) onProgress(parsed);
      } else if (eventName === 'done' || parsed.success !== undefined) {
        result = parsed;
      }
    });
    return result || { success: false, errors: ['No result received'] };
  },

  /**
   * Full auto flow: download → fill → upload via SSE stream.
   * @param {object} opts - { draftGroupId, sport, lineupPlayerIds }
   * @param {function} onProgress - Called with { step, detail }
   * @returns {Promise<object>} DKFullResult
   */
  async dkAutoUpload({ draftGroupId, sport = 'cbb', lineupPlayerIds } = {}, onProgress) {
    const res = await authFetch(`${API_BASE}/dk-entries/auto`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        draft_group_id: draftGroupId,
        sport,
        lineup_player_ids: lineupPlayerIds,
      }),
    });
    if (!res.ok) {
      throw new Error(await extractError(res, 'Auto upload failed'));
    }
    let result = null;
    await _parseSSEStream(res.body.getReader(), (parsed, _raw, eventName) => {
      if (!parsed) return;
      if (eventName === 'error' || parsed.error) {
        throw new Error(parsed?.error || 'Auto upload failed');
      }
      if (eventName === 'progress' || parsed.step) {
        if (onProgress) onProgress(parsed);
      } else if (eventName === 'done' || parsed.download_ok !== undefined) {
        result = parsed;
      }
    });
    return result || { download_ok: false, error: 'No result received' };
  },

  /** Import a projection CSV scoped to one sport.
   * @param {File} file
   * @param {string} sport - 'nba' (default) | 'cbb' | 'nfl' | 'mlb'
   */
  async importProjections(file, sport = 'nba') {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('sport', sport);
    const url = `${API_BASE}/player-pool/import-projections?sport=${encodeURIComponent(sport)}`;
    const res = await authFetch(url, {
      method: 'POST',
      body: formData,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: 'Upload failed' }));
      throw new Error(err.detail || 'Failed to import projections');
    }
    return res.json();
  },

  /** List saved CSV-name → pool-player aliases.
   * @param {string} [sport] — when given, returns just that sport's bucket.
   */
  async listProjectionAliases(sport) {
    const url = sport
      ? `${API_BASE}/player-pool/projection-aliases?sport=${encodeURIComponent(sport)}`
      : `${API_BASE}/player-pool/projection-aliases`
    const res = await authFetch(url)
    if (!res.ok) throw new Error(await extractError(res, 'List aliases failed'))
    return res.json()
  },

  /** Persist a manual CSV → pool-player match for one sport. */
  async addProjectionAlias({ csvName, playerId, canonicalName, sport = 'nba' } = {}) {
    const body = { csv_name: csvName, sport }
    if (playerId !== undefined) body.player_id = playerId
    if (canonicalName) body.canonical_name = canonicalName
    const url = `${API_BASE}/player-pool/projection-aliases?sport=${encodeURIComponent(sport)}`
    const res = await authFetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) throw new Error(await extractError(res, 'Save alias failed'))
    return res.json()
  },

  async deleteProjectionAlias(csvName, sport = 'nba') {
    const url = `${API_BASE}/player-pool/projection-aliases/${encodeURIComponent(csvName)}?sport=${encodeURIComponent(sport)}`
    const res = await authFetch(url, { method: 'DELETE' })
    if (!res.ok) throw new Error(await extractError(res, 'Delete alias failed'))
    return res.json()
  },

  async clearImportedProjections() {
    const res = await authFetch(`${API_BASE}/player-pool/import-projections`, {
      method: 'DELETE',
    });
    if (!res.ok) throw new Error('Failed to clear imported projections');
    return res.json();
  },
};

export default rotationAPI;
