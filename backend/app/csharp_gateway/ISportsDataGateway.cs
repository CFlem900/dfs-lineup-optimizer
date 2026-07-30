// ============================================================================
// BallDontLie MCP Gateway — C# Interfaces & DTOs
// ============================================================================
//
// ARCHITECTURE NOTE:
//
// The BDL MCP exposes exactly FOUR tools:
//
//   get_teams(league)                         → List<BdlTeam>
//   get_players(league, firstName?, lastName?) → List<BdlPlayer>
//   get_games(league, dates?, seasons?, teamIds?) → List<BdlGame>
//   get_game(league, gameId)                  → BdlGame?
//
// It does NOT support: GetStandings, GetOdds, GetPlayerProps, GetStats,
// GetBoxScores, GetBettingLines, or any other endpoint.
//
// The "league" parameter is a string enum: "NBA", "NFL", "MLB".
// All four tools share the same response shapes — the league param
// filters which data is returned, NOT which schema.
//
// This means:
//   - GetStandings<T> CANNOT be implemented via MCP (no standings endpoint)
//   - GetOdds<T> CANNOT be implemented via MCP (no odds endpoint)
//   - A generic T per league is unnecessary — the shapes are uniform
//   - The correct generic pattern is per-OPERATION, not per-league
//
// What IS correct:
//   - IBdlMcpGateway with 4 typed methods matching the 4 MCP tools
//   - Circuit breaker around the sidecar HTTP calls
//   - A SEPARATE IOddsProvider / IStandingsProvider backed by
//     DraftKings, ESPN, or the BDL REST API (not MCP)
//
// ============================================================================

using System.Text.Json.Serialization;

namespace Prediction.SportsData.Gateway;

// ── League enum (matches MCP parameter) ─────────────────────────────────

public enum League
{
    NBA,
    NFL,
    MLB
}

// ============================================================================
// DTOs — mirror Python Pydantic schemas (balldontlie_schemas.py)
// ============================================================================

/// <summary>
/// Team from BDL MCP get_teams / nested in game+player responses.
/// Maps to Python: BDLTeam
/// </summary>
public sealed record BdlTeam
{
    [JsonPropertyName("id")]
    public int Id { get; init; }

    [JsonPropertyName("abbreviation")]
    public string Abbreviation { get; init; } = "";

    [JsonPropertyName("city")]
    public string? City { get; init; }

    [JsonPropertyName("full_name")]
    public string? FullName { get; init; }

    [JsonPropertyName("name")]
    public string? Name { get; init; }

    [JsonPropertyName("conference")]
    public string? Conference { get; init; }

    [JsonPropertyName("division")]
    public string? Division { get; init; }
}

/// <summary>
/// Player from BDL MCP get_players.
/// Maps to Python: BDLPlayer
/// </summary>
public sealed record BdlPlayer
{
    [JsonPropertyName("id")]
    public int Id { get; init; }

    [JsonPropertyName("first_name")]
    public string FirstName { get; init; } = "";

    [JsonPropertyName("last_name")]
    public string LastName { get; init; } = "";

    [JsonPropertyName("position")]
    public string? Position { get; init; }

    [JsonPropertyName("height")]
    public string? Height { get; init; }

    [JsonPropertyName("weight")]
    public int? Weight { get; init; }

    [JsonPropertyName("jersey_number")]
    public string? JerseyNumber { get; init; }

    [JsonPropertyName("team")]
    public BdlTeam? Team { get; init; }

    [JsonIgnore]
    public string FullName => $"{FirstName} {LastName}".Trim();
}

/// <summary>
/// Game from BDL MCP get_games / get_game.
/// Maps to Python: BDLGame
/// </summary>
public sealed record BdlGame
{
    [JsonPropertyName("id")]
    public int Id { get; init; }

    [JsonPropertyName("home_team")]
    public BdlTeam HomeTeam { get; init; } = new();

    [JsonPropertyName("visitor_team")]
    public BdlTeam VisitorTeam { get; init; } = new();

    [JsonPropertyName("status")]
    public string? Status { get; init; }

    [JsonPropertyName("period")]
    public int? Period { get; init; }

    [JsonPropertyName("time")]
    public string? Time { get; init; }

    [JsonPropertyName("home_team_score")]
    public int? HomeTeamScore { get; init; }

    [JsonPropertyName("visitor_team_score")]
    public int? VisitorTeamScore { get; init; }

    [JsonPropertyName("date")]
    public string? Date { get; init; }

    [JsonPropertyName("season")]
    public int? Season { get; init; }

    [JsonPropertyName("postseason")]
    public bool? Postseason { get; init; }
}

// ============================================================================
// MCP Gateway Interface — the 4 real tools
// ============================================================================

/// <summary>
/// Typed C# interface mirroring the BDL MCP's actual tool surface.
///
/// Each method maps 1:1 to an MCP tool. The league parameter selects
/// NBA/NFL/MLB data — the response DTOs are the same across leagues.
///
/// Circuit breaker is applied at the implementation level (see
/// <see cref="BdlMcpGateway"/>).
/// </summary>
public interface IBdlMcpGateway
{
    /// <summary>MCP tool: get_teams</summary>
    Task<IReadOnlyList<BdlTeam>> GetTeamsAsync(
        League league,
        CancellationToken ct = default);

    /// <summary>MCP tool: get_players</summary>
    Task<IReadOnlyList<BdlPlayer>> GetPlayersAsync(
        League league,
        string? firstName = null,
        string? lastName = null,
        CancellationToken ct = default);

    /// <summary>MCP tool: get_games</summary>
    Task<IReadOnlyList<BdlGame>> GetGamesAsync(
        League league,
        string[]? dates = null,
        int[]? seasons = null,
        int[]? teamIds = null,
        CancellationToken ct = default);

    /// <summary>MCP tool: get_game</summary>
    Task<BdlGame?> GetGameAsync(
        League league,
        int gameId,
        CancellationToken ct = default);
}

// ============================================================================
// NOT AVAILABLE via MCP — separate provider interfaces
// ============================================================================

/// <summary>
/// Odds/betting data is NOT available through the BDL MCP.
///
/// The Python backend sources odds from:
///   - BallDontLieService REST API (/v2/odds) — NOT the MCP
///   - DKPropsService (DraftKings Sportsbook scraper)
///
/// This interface represents a generic odds provider that your C# code
/// should implement against whatever REST source provides the data.
/// </summary>
public interface IOddsProvider
{
    Task<IReadOnlyList<GameOdds>> GetGameOddsAsync(
        League league,
        string gameDate,
        CancellationToken ct = default);
}

/// <summary>
/// Odds DTO — sourced from BDL REST /v2/odds or DraftKings, NOT the MCP.
/// Maps to Python: BDLOddsEntry
/// </summary>
public sealed record GameOdds
{
    public int GameId { get; init; }
    public string? Vendor { get; init; }
    public double? SpreadHome { get; init; }
    public double? SpreadAway { get; init; }
    public double? Total { get; init; }
    public int? MoneylineHome { get; init; }
    public int? MoneylineAway { get; init; }
    public string? UpdatedAt { get; init; }
}

/// <summary>
/// Standings are NOT available through any BDL endpoint (MCP or REST).
/// You would source these from ESPN, NBA.com, or a similar API.
/// </summary>
public interface IStandingsProvider
{
    Task<IReadOnlyList<TeamStanding>> GetStandingsAsync(
        League league,
        int season,
        CancellationToken ct = default);
}

public sealed record TeamStanding
{
    public string TeamAbbreviation { get; init; } = "";
    public int Wins { get; init; }
    public int Losses { get; init; }
    public double WinPct { get; init; }
    public string? Conference { get; init; }
    public int? ConferenceRank { get; init; }
}
