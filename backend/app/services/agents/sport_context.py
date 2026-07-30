"""Sport-aware context preamble for AI agents.

Provides a concise CBB context paragraph that gets prepended to any
agent's system prompt when ``sport='cbb'``.  This avoids rewriting
every agent while ensuring they reason about college basketball
dynamics (shorter games, different pace, different roster sizes, etc.)
instead of defaulting to NBA assumptions.

Usage in agents::

    from app.services.agents.sport_context import get_sport_preamble

    system_prompt = get_sport_preamble(sport) + ORIGINAL_SYSTEM_PROMPT
"""


def get_sport_preamble(sport: str = "nba") -> str:
    """Return a sport-context preamble for AI agent system prompts.

    Parameters
    ----------
    sport : str
        ``"nba"`` (default) returns empty string.
        ``"cbb"`` returns a CBB context paragraph.

    Returns
    -------
    str
        Preamble text to prepend to the agent's system prompt.
        Empty string for NBA (no modification needed).
    """
    if sport == "cbb":
        return (
            "SPORT CONTEXT: NCAA Men's College Basketball (CBB).\n"
            "Key differences from NBA:\n"
            "- 40-minute games (not 48), 200 total team-minutes (not 240)\n"
            "- Average pace ~68 possessions/game (vs ~102 NBA)\n"
            "- Deeper rotations common (8-10 players), but less predictable\n"
            "- No back-to-backs; conference tournament and March Madness scheduling\n"
            "- Positions less rigid (G, F, C vs PG/SG/SF/PF/C)\n"
            "- Younger, less consistent players; higher variance in performance\n"
            "- Conference strength matters significantly for matchup analysis\n"
            "- Shot clock is 30 seconds (not 24), affecting tempo\n"
            "- Fewer minutes per player = smaller DFS scoring floors\n"
            "Adjust all analysis for these CBB-specific dynamics.\n\n"
        )
    return ""
