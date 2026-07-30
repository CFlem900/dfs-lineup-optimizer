// ============================================================================
// BdlMcpGateway — Implementation with Circuit Breaker
// ============================================================================
//
// Calls the Python sidecar API (FastAPI) which wraps the BDL MCP tools.
// Circuit breaker mirrors the Python-side pattern in http_resilience.py:
//
//   CLOSED → (5 consecutive failures) → OPEN → (30s cooldown) → HALF_OPEN
//                                                                   ↓
//                                                          success → CLOSED
//                                                          failure → OPEN
//
// The sidecar exposes endpoints like:
//   GET /api/bdl-mcp/teams?league=NBA
//   GET /api/bdl-mcp/players?league=NFL&lastName=Mahomes
//   GET /api/bdl-mcp/games?league=NBA&dates=2026-02-25
//   GET /api/bdl-mcp/game/9001?league=NBA
//
// These map 1:1 to the 4 MCP tools. There are no other endpoints.
// ============================================================================

using System.Net.Http.Json;
using System.Text.Json;
using Microsoft.Extensions.Logging;

namespace Prediction.SportsData.Gateway;

// ── Circuit Breaker ─────────────────────────────────────────────────────

public sealed class CircuitBreaker
{
    public enum State { Closed, Open, HalfOpen }

    private readonly int _failureThreshold;
    private readonly TimeSpan _recoveryWindow;
    private readonly object _lock = new();

    private State _state = State.Closed;
    private int _consecutiveFailures;
    private DateTimeOffset _openedAt;

    public CircuitBreaker(int failureThreshold = 5, TimeSpan? recoveryWindow = null)
    {
        _failureThreshold = failureThreshold;
        _recoveryWindow = recoveryWindow ?? TimeSpan.FromSeconds(30);
    }

    public State CurrentState
    {
        get
        {
            lock (_lock)
            {
                if (_state == State.Open &&
                    DateTimeOffset.UtcNow - _openedAt >= _recoveryWindow)
                {
                    _state = State.HalfOpen;
                }
                return _state;
            }
        }
    }

    public bool AllowRequest => CurrentState != State.Open;

    public void RecordSuccess()
    {
        lock (_lock)
        {
            _consecutiveFailures = 0;
            _state = State.Closed;
        }
    }

    public void RecordFailure()
    {
        lock (_lock)
        {
            _consecutiveFailures++;
            if (_consecutiveFailures >= _failureThreshold)
            {
                _state = State.Open;
                _openedAt = DateTimeOffset.UtcNow;
            }
        }
    }
}

public sealed class CircuitBreakerOpenException : Exception
{
    public CircuitBreakerOpenException(string source)
        : base($"Circuit breaker OPEN for {source}. Failing fast.") { }
}

// ── Gateway Implementation ──────────────────────────────────────────────

public sealed class BdlMcpGateway : IBdlMcpGateway
{
    private readonly HttpClient _http;
    private readonly CircuitBreaker _breaker;
    private readonly ILogger<BdlMcpGateway> _logger;
    private readonly string _baseUrl;

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    };

    public BdlMcpGateway(
        HttpClient http,
        ILogger<BdlMcpGateway> logger,
        string sidecarBaseUrl = "http://localhost:8000")
    {
        _http = http;
        _logger = logger;
        _baseUrl = sidecarBaseUrl.TrimEnd('/');
        _breaker = new CircuitBreaker(failureThreshold: 5,
                                       recoveryWindow: TimeSpan.FromSeconds(30));
    }

    // ── MCP Tool 1: get_teams ───────────────────────────────────────

    public async Task<IReadOnlyList<BdlTeam>> GetTeamsAsync(
        League league, CancellationToken ct = default)
    {
        var url = $"{_baseUrl}/api/bdl-mcp/teams?league={league}";
        var result = await CallWithBreakerAsync<List<BdlTeam>>(url, ct);
        return result ?? [];
    }

    // ── MCP Tool 2: get_players ─────────────────────────────────────

    public async Task<IReadOnlyList<BdlPlayer>> GetPlayersAsync(
        League league,
        string? firstName = null,
        string? lastName = null,
        CancellationToken ct = default)
    {
        var url = $"{_baseUrl}/api/bdl-mcp/players?league={league}";
        if (!string.IsNullOrEmpty(firstName)) url += $"&firstName={Uri.EscapeDataString(firstName)}";
        if (!string.IsNullOrEmpty(lastName))  url += $"&lastName={Uri.EscapeDataString(lastName)}";

        var result = await CallWithBreakerAsync<List<BdlPlayer>>(url, ct);
        return result ?? [];
    }

    // ── MCP Tool 3: get_games ───────────────────────────────────────

    public async Task<IReadOnlyList<BdlGame>> GetGamesAsync(
        League league,
        string[]? dates = null,
        int[]? seasons = null,
        int[]? teamIds = null,
        CancellationToken ct = default)
    {
        var url = $"{_baseUrl}/api/bdl-mcp/games?league={league}";
        if (dates is not null)
            foreach (var d in dates) url += $"&dates={Uri.EscapeDataString(d)}";
        if (seasons is not null)
            foreach (var s in seasons) url += $"&seasons={s}";
        if (teamIds is not null)
            foreach (var t in teamIds) url += $"&teamIds={t}";

        var result = await CallWithBreakerAsync<List<BdlGame>>(url, ct);
        return result ?? [];
    }

    // ── MCP Tool 4: get_game ────────────────────────────────────────

    public async Task<BdlGame?> GetGameAsync(
        League league, int gameId, CancellationToken ct = default)
    {
        var url = $"{_baseUrl}/api/bdl-mcp/game/{gameId}?league={league}";
        return await CallWithBreakerAsync<BdlGame>(url, ct);
    }

    // ── Circuit-breaker-wrapped HTTP call ────────────────────────────

    private async Task<T?> CallWithBreakerAsync<T>(
        string url, CancellationToken ct) where T : class
    {
        if (!_breaker.AllowRequest)
        {
            _logger.LogWarning("[BdlMcpGateway] Circuit breaker OPEN — failing fast for {Url}", url);
            throw new CircuitBreakerOpenException("BDL MCP Sidecar");
        }

        try
        {
            var response = await _http.GetAsync(url, ct);
            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<T>(JsonOpts, ct);
            _breaker.RecordSuccess();
            return result;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            _breaker.RecordFailure();
            _logger.LogError(ex, "[BdlMcpGateway] Request failed: {Url}", url);
            throw;
        }
    }
}

// ============================================================================
// Usage Examples
// ============================================================================
//
// ── Example 1: Fetch NBA teams ─────────────────────────────────────────
//
//   var gateway = serviceProvider.GetRequiredService<IBdlMcpGateway>();
//   var nbaTeams = await gateway.GetTeamsAsync(League.NBA);
//   var nflTeams = await gateway.GetTeamsAsync(League.NFL);
//   // Same method, same return type — league param filters the data
//
// ── Example 2: Search NFL players ──────────────────────────────────────
//
//   var players = await gateway.GetPlayersAsync(
//       League.NFL, lastName: "Mahomes");
//
// ── Example 3: Get today's MLB games ───────────────────────────────────
//
//   var games = await gateway.GetGamesAsync(
//       League.MLB, dates: ["2026-02-25"]);
//
// ── Example 4: WRONG — Odds are NOT available via MCP ──────────────────
//
//   // This does NOT work — there is no MCP odds tool:
//   // var odds = await gateway.GetOddsAsync(League.NFL, "2026-02-25");
//   //
//   // Instead, use a separate IOddsProvider backed by:
//   //   - Python sidecar REST: /api/odds?date=2026-02-25 (BDL V2 REST)
//   //   - DraftKings API directly
//   //
//   var oddsProvider = serviceProvider.GetRequiredService<IOddsProvider>();
//   var nflOdds = await oddsProvider.GetGameOddsAsync(
//       League.NFL, "2026-02-25");
//
// ── Example 5: DI Registration ─────────────────────────────────────────
//
//   // In Program.cs / Startup.cs:
//   services.AddHttpClient<IBdlMcpGateway, BdlMcpGateway>(client =>
//   {
//       client.BaseAddress = new Uri("http://localhost:8000");
//       client.Timeout = TimeSpan.FromSeconds(12);
//   });
//
// ============================================================================
