"""Expert source registry for NBA analyst signal tracking.

Defines tracked expert accounts (Twitter/web) with tier classification
and specialty tags. Easily extensible — add new entries to EXPERT_REGISTRY.

Last updated: Feb 2026
  - Removed @wojespn (Adrian Wojnarowski retired Sept 2024)
  - Promoted Chris Haynes to Tier 1
  - Demoted Zach Lowe to Tier 2 (left ESPN late 2023, lower posting frequency)
  - Added Marc Stein and Bobby Marks
  - Added DFS-focused experts: Awesemo, Stokastic NBA, RG_Notorious,
    headchopper, naapstermaan, beermakersfan, RotoGrinders
"""

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel


class ExpertTier(str, Enum):
    TIER_1 = "tier_1"  # Top-level (Shams, Hollinger, Haynes)
    TIER_2 = "tier_2"  # Strong analysts (beat reporters, established fantasy)
    TIER_3 = "tier_3"  # Niche/rotation-specific voices


class ExpertSource(BaseModel):
    name: str
    handle: str                          # Twitter/X handle (without @)
    platform: Literal["twitter", "web"]
    url: Optional[str] = None            # Website URL if platform is "web"
    tier: ExpertTier
    specialty: Literal[
        "breaking_news", "fantasy", "rotations", "analytics", "beat_reporter"
    ]
    team_affiliation: Optional[str] = None  # e.g., "LAL" for beat reporters
    avatar_url: Optional[str] = None
    verified: bool = True


EXPERT_REGISTRY: list[ExpertSource] = [
    # -- Tier 1 --- Breaking News / Top Analysts ----------------------------
    ExpertSource(
        name="Shams Charania", handle="ShamsCharania",
        platform="twitter", tier=ExpertTier.TIER_1, specialty="breaking_news",
    ),
    ExpertSource(
        name="Chris Haynes", handle="ChrisBHaynes",
        platform="twitter", tier=ExpertTier.TIER_1, specialty="breaking_news",
    ),
    ExpertSource(
        name="John Hollinger", handle="johnhollinger",
        platform="twitter", tier=ExpertTier.TIER_1, specialty="analytics",
    ),
    ExpertSource(
        name="Kevin O'Connor", handle="KevinOConnorNBA",
        platform="twitter", tier=ExpertTier.TIER_1, specialty="analytics",
    ),

    # -- Tier 2 --- Fantasy / Rotation Specialists --------------------------
    ExpertSource(
        name="Zach Lowe", handle="ZachLowe_NBA",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="analytics",
    ),
    ExpertSource(
        name="Josh Lloyd", handle="redikiux",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="fantasy",
    ),
    ExpertSource(
        name="Josh Eberley", handle="JoshEberley",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="fantasy",
    ),
    ExpertSource(
        name="Mike Gallagher", handle="MikeSGallagher",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="rotations",
    ),
    ExpertSource(
        name="Locked On Fantasy", handle="LockedOnFBball",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="fantasy",
    ),
    ExpertSource(
        name="Jake Fischer", handle="JakeLFischer",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="breaking_news",
    ),
    ExpertSource(
        name="Marc Stein", handle="TheSteinLine",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="breaking_news",
    ),
    ExpertSource(
        name="Bobby Marks", handle="BobbyMarks42",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="analytics",
    ),

    # -- Tier 2 --- DFS-Focused Experts ---------------------------------------
    ExpertSource(
        name="Awesemo (Alex Baker)", handle="AwesemoDFS",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="fantasy",
    ),
    ExpertSource(
        name="Stokastic NBA DFS", handle="StokasticNBA",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="fantasy",
    ),
    ExpertSource(
        name="Derek Farnsworth", handle="RG_Notorious",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="fantasy",
    ),
    ExpertSource(
        name="David Kaplen", handle="headchopper",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="fantasy",
    ),
    ExpertSource(
        name="Erik Wardenburg", handle="naapstermaan",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="rotations",
    ),
    ExpertSource(
        name="Chris Prince", handle="beermakersfan",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="fantasy",
    ),
    ExpertSource(
        name="RotoGrinders", handle="RotoGrinders",
        platform="twitter", tier=ExpertTier.TIER_2, specialty="fantasy",
    ),

    # -- Tier 3 --- Niche / Rotation-focused --------------------------------
    ExpertSource(
        name="Basketball Monster", handle="BaskMonster",
        platform="twitter", tier=ExpertTier.TIER_3, specialty="fantasy",
    ),
    ExpertSource(
        name="Hashtag Basketball", handle="HashtagBball",
        platform="twitter", tier=ExpertTier.TIER_3, specialty="rotations",
    ),

    # -- Web Sources --------------------------------------------------------
    ExpertSource(
        name="RotoWire Blurbs", handle="rotowire", platform="web",
        url="https://www.rotowire.com/basketball/nba-lineups.php",
        tier=ExpertTier.TIER_2, specialty="rotations",
    ),
    ExpertSource(
        name="FantasyLabs Projections", handle="fantasylabs", platform="web",
        url="https://www.fantasylabs.com/nba/projections/",
        tier=ExpertTier.TIER_2, specialty="fantasy",
    ),
]


def get_twitter_experts(min_tier: ExpertTier = ExpertTier.TIER_3) -> list[ExpertSource]:
    """Return Twitter experts at or above a given tier."""
    tier_order = {ExpertTier.TIER_1: 1, ExpertTier.TIER_2: 2, ExpertTier.TIER_3: 3}
    max_val = tier_order[min_tier]
    return [
        e for e in EXPERT_REGISTRY
        if e.platform == "twitter" and tier_order[e.tier] <= max_val
    ]


def get_web_experts() -> list[ExpertSource]:
    """Return web-based expert sources."""
    return [e for e in EXPERT_REGISTRY if e.platform == "web"]
