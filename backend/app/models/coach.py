from pydantic import BaseModel
from typing import Dict
from enum import Enum


class CoachingStyle(str, Enum):
    HEAVY_MINUTES = "heavy_minutes"
    BALANCED = "balanced"
    DEEP_ROTATION = "deep_rotation"
    STAR_DEPENDENT = "star_dependent"
    DEVELOPMENTAL = "developmental"


class CoachProfile(BaseModel):
    coach_name: str
    team_id: int
    style: CoachingStyle
    starter_multiplier: float = 1.0
    bench_multiplier: float = 1.0
    star_multiplier: float = 1.0
    max_minutes_override: float = 42.0
    min_rotation_size: int = 8
    blowout_threshold: int = 20
    garbage_time_redistribution: bool = True


# ==========================================================================
# All 30 NBA teams — 2025-26 season (updated Feb 2026)
#
# Coaching changes from 2024-25 offseason:
#   NYK: Tom Thibodeau fired → Mike Brown hired (Jul 2025)
#   PHX: Mike Budenholzer fired → Jordan Ott hired
#   DEN: Michael Malone fired → David Adelman promoted from interim
#   MEM: Taylor Jenkins fired → Tuomas Iisalo promoted from interim
#   NOP: Willie Green fired (2-10 start) → James Borrego interim
#   SAS: Mitch Johnson promoted to full-time HC (Pop officially retired)
#   SAC: Doug Christie confirmed as full-time HC (May 2025)
# ==========================================================================

COACH_PROFILES: Dict[str, CoachProfile] = {
    # ----- HEAVY MINUTES -----
    "Nick Nurse": CoachProfile(
        coach_name="Nick Nurse",
        team_id=1610612755,  # PHI
        style=CoachingStyle.HEAVY_MINUTES,
        starter_multiplier=1.12,
        bench_multiplier=0.88,
        star_multiplier=1.15,
        max_minutes_override=42.0,
        min_rotation_size=8,
    ),
    # ----- STAR DEPENDENT -----
    "Jason Kidd": CoachProfile(
        coach_name="Jason Kidd",
        team_id=1610612742,  # DAL
        style=CoachingStyle.STAR_DEPENDENT,
        starter_multiplier=1.10,
        bench_multiplier=0.88,
        star_multiplier=1.18,
        max_minutes_override=42.0,
        min_rotation_size=8,
    ),
    "Doc Rivers": CoachProfile(
        coach_name="Doc Rivers",
        team_id=1610612749,  # MIL
        style=CoachingStyle.STAR_DEPENDENT,
        starter_multiplier=1.10,
        bench_multiplier=0.88,
        star_multiplier=1.15,
        max_minutes_override=42.0,
        min_rotation_size=8,
    ),
    # ----- BALANCED -----
    "Mike Brown": CoachProfile(
        # Hired Jul 2025, replaced Tom Thibodeau
        # Two-time Coach of the Year; historically runs balanced rotations
        # with star emphasis — less extreme than Thibs but still leans on starters
        coach_name="Mike Brown",
        team_id=1610612752,  # NYK
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.08,
        bench_multiplier=0.95,
        star_multiplier=1.12,
        max_minutes_override=40.0,
        min_rotation_size=9,
    ),
    "Erik Spoelstra": CoachProfile(
        coach_name="Erik Spoelstra",
        team_id=1610612748,  # MIA
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=1.05,
        star_multiplier=1.05,
        max_minutes_override=40.0,
        min_rotation_size=9,
    ),
    "Joe Mazzulla": CoachProfile(
        coach_name="Joe Mazzulla",
        team_id=1610612738,  # BOS
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=1.02,
        star_multiplier=1.08,
        max_minutes_override=38.0,
        min_rotation_size=9,
    ),
    "Steve Kerr": CoachProfile(
        coach_name="Steve Kerr",
        team_id=1610612744,  # GSW
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=1.05,
        star_multiplier=1.08,
        max_minutes_override=38.0,
        min_rotation_size=9,
    ),
    "Tyronn Lue": CoachProfile(
        coach_name="Tyronn Lue",
        team_id=1610612746,  # LAC
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=1.00,
        star_multiplier=1.08,
        max_minutes_override=40.0,
        min_rotation_size=9,
    ),
    "David Adelman": CoachProfile(
        # Promoted from interim May 2025, replaced Michael Malone
        # First full-time gig; expected to continue Malone's balanced approach
        coach_name="David Adelman",
        team_id=1610612743,  # DEN
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=1.00,
        star_multiplier=1.10,
        max_minutes_override=40.0,
        min_rotation_size=9,
    ),
    "Rick Carlisle": CoachProfile(
        coach_name="Rick Carlisle",
        team_id=1610612754,  # IND
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=1.00,
        star_multiplier=1.05,
        max_minutes_override=40.0,
        min_rotation_size=9,
    ),
    "Billy Donovan": CoachProfile(
        coach_name="Billy Donovan",
        team_id=1610612741,  # CHI
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.03,
        bench_multiplier=1.02,
        star_multiplier=1.05,
        max_minutes_override=38.0,
        min_rotation_size=9,
    ),
    "Chris Finch": CoachProfile(
        coach_name="Chris Finch",
        team_id=1610612750,  # MIN
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=1.00,
        star_multiplier=1.08,
        max_minutes_override=40.0,
        min_rotation_size=9,
    ),
    "JJ Redick": CoachProfile(
        coach_name="JJ Redick",
        team_id=1610612747,  # LAL
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=1.00,
        star_multiplier=1.10,
        max_minutes_override=40.0,
        min_rotation_size=9,
    ),
    "Kenny Atkinson": CoachProfile(
        coach_name="Kenny Atkinson",
        team_id=1610612739,  # CLE
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=1.02,
        star_multiplier=1.05,
        max_minutes_override=38.0,
        min_rotation_size=9,
    ),
    "Quin Snyder": CoachProfile(
        coach_name="Quin Snyder",
        team_id=1610612737,  # ATL
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=1.00,
        star_multiplier=1.08,
        max_minutes_override=40.0,
        min_rotation_size=9,
    ),
    "Jamahl Mosley": CoachProfile(
        coach_name="Jamahl Mosley",
        team_id=1610612753,  # ORL
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=1.00,
        star_multiplier=1.05,
        max_minutes_override=38.0,
        min_rotation_size=9,
    ),
    "Tuomas Iisalo": CoachProfile(
        # Promoted from interim after Taylor Jenkins fired late 2024-25
        # Finnish coach; analytical background, expected to be balanced
        coach_name="Tuomas Iisalo",
        team_id=1610612763,  # MEM
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=1.00,
        star_multiplier=1.08,
        max_minutes_override=40.0,
        min_rotation_size=9,
    ),
    "Doug Christie": CoachProfile(
        # Confirmed full-time HC May 2025 (replaced Mike Brown Dec 2024)
        coach_name="Doug Christie",
        team_id=1610612758,  # SAC
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=1.00,
        star_multiplier=1.08,
        max_minutes_override=40.0,
        min_rotation_size=9,
    ),
    # ----- DEEP ROTATION -----
    "Mark Daigneault": CoachProfile(
        coach_name="Mark Daigneault",
        team_id=1610612760,  # OKC
        style=CoachingStyle.DEEP_ROTATION,
        starter_multiplier=0.98,
        bench_multiplier=1.10,
        star_multiplier=1.00,
        max_minutes_override=36.0,
        min_rotation_size=10,
    ),
    "Ime Udoka": CoachProfile(
        coach_name="Ime Udoka",
        team_id=1610612745,  # HOU
        style=CoachingStyle.DEEP_ROTATION,
        starter_multiplier=1.00,
        bench_multiplier=1.08,
        star_multiplier=1.02,
        max_minutes_override=38.0,
        min_rotation_size=10,
    ),
    "Mitch Johnson": CoachProfile(
        # Full-time HC since May 2025 (succeeded Gregg Popovich)
        coach_name="Mitch Johnson",
        team_id=1610612759,  # SAS
        style=CoachingStyle.DEEP_ROTATION,
        starter_multiplier=0.98,
        bench_multiplier=1.08,
        star_multiplier=1.05,
        max_minutes_override=38.0,
        min_rotation_size=10,
    ),
    "Will Hardy": CoachProfile(
        coach_name="Will Hardy",
        team_id=1610612762,  # UTA
        style=CoachingStyle.DEEP_ROTATION,
        starter_multiplier=0.98,
        bench_multiplier=1.08,
        star_multiplier=0.95,
        max_minutes_override=36.0,
        min_rotation_size=10,
    ),
    "Jordan Ott": CoachProfile(
        # Hired 2025, replaced Mike Budenholzer
        # First-time HC from Cleveland Cavaliers assistant staff
        # Style TBD — defaulting to balanced with slight star emphasis given PHX roster
        coach_name="Jordan Ott",
        team_id=1610612756,  # PHX
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.05,
        bench_multiplier=0.95,
        star_multiplier=1.10,
        max_minutes_override=40.0,
        min_rotation_size=9,
    ),
    # ----- DEVELOPMENTAL -----
    "Brian Keefe": CoachProfile(
        coach_name="Brian Keefe",
        team_id=1610612764,  # WAS
        style=CoachingStyle.DEVELOPMENTAL,
        starter_multiplier=0.95,
        bench_multiplier=1.10,
        star_multiplier=1.00,
        max_minutes_override=36.0,
        min_rotation_size=10,
    ),
    "Jordi Fernandez": CoachProfile(
        coach_name="Jordi Fernandez",
        team_id=1610612751,  # BKN
        style=CoachingStyle.DEVELOPMENTAL,
        starter_multiplier=0.95,
        bench_multiplier=1.10,
        star_multiplier=0.98,
        max_minutes_override=36.0,
        min_rotation_size=10,
    ),
    "Charles Lee": CoachProfile(
        coach_name="Charles Lee",
        team_id=1610612766,  # CHA
        style=CoachingStyle.DEVELOPMENTAL,
        starter_multiplier=0.98,
        bench_multiplier=1.08,
        star_multiplier=1.00,
        max_minutes_override=36.0,
        min_rotation_size=10,
    ),
    "J.B. Bickerstaff": CoachProfile(
        coach_name="J.B. Bickerstaff",
        team_id=1610612765,  # DET
        style=CoachingStyle.DEVELOPMENTAL,
        starter_multiplier=0.98,
        bench_multiplier=1.08,
        star_multiplier=1.02,
        max_minutes_override=38.0,
        min_rotation_size=10,
    ),
    "Darko Rajakovic": CoachProfile(
        coach_name="Darko Rajakovic",
        team_id=1610612761,  # TOR
        style=CoachingStyle.DEVELOPMENTAL,
        starter_multiplier=0.98,
        bench_multiplier=1.08,
        star_multiplier=1.00,
        max_minutes_override=36.0,
        min_rotation_size=10,
    ),
    "Chauncey Billups": CoachProfile(
        coach_name="Chauncey Billups",
        team_id=1610612757,  # POR
        style=CoachingStyle.DEVELOPMENTAL,
        starter_multiplier=0.98,
        bench_multiplier=1.08,
        star_multiplier=1.02,
        max_minutes_override=38.0,
        min_rotation_size=10,
    ),
    "James Borrego": CoachProfile(
        # Interim HC since Nov 2025 (replaced Willie Green after 2-10 start)
        # Former CHA head coach; leans developmental while evaluating roster
        coach_name="James Borrego",
        team_id=1610612740,  # NOP
        style=CoachingStyle.DEVELOPMENTAL,
        starter_multiplier=0.98,
        bench_multiplier=1.08,
        star_multiplier=1.00,
        max_minutes_override=38.0,
        min_rotation_size=10,
    ),
    # ----- DEFAULT FALLBACK -----
    "Default": CoachProfile(
        coach_name="Default",
        team_id=0,
        style=CoachingStyle.BALANCED,
        starter_multiplier=1.0,
        bench_multiplier=1.0,
        star_multiplier=1.0,
        max_minutes_override=42.0,
        min_rotation_size=8,
    ),
}


def get_coach_profile(team_id: int) -> CoachProfile:
    """Look up coach profile by team ID. Falls back to Default."""
    for profile in COACH_PROFILES.values():
        if profile.team_id == team_id:
            return profile
    return COACH_PROFILES["Default"]
