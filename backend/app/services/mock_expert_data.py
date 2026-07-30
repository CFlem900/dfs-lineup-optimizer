"""Mock expert signal data generator for development and testing.

Generates realistic-looking expert signals without hitting any external
APIs or websites. Enable by setting SCRAPING_ENABLED=false in .env.
"""

import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.config.expert_sources import EXPERT_REGISTRY, ExpertTier
from app.models.expert_signal import ExpertSignal

# ── Template pools ─────────────────────────────────────────────────
INJURY_TEMPLATES = [
    "{player} is listed as questionable for tonight with a knee issue.",
    "Sources: {player} expected to miss tonight's game ({team} vs opponent). Knee soreness.",
    "{player} has been upgraded to probable for tonight. Should be good to go.",
    "{player} ruled out for tonight. {team} will need to adjust their rotation.",
    "Hearing {player} is day-to-day with a minor ankle sprain. Will be re-evaluated.",
]

ROTATION_TEMPLATES = [
    "{player} seeing expanded role lately — 32+ mins in 3 of last 5 games for {team}.",
    "Coach giving {player} more run at the 4 spot. Interesting rotation shift for {team}.",
    "{player} moved to bench in favor of the hot hand. Monitor minutes going forward.",
    "With the injury, expect {player} to slide into the starting lineup tonight for {team}.",
    "{player} has been the primary ball handler in crunch time recently. Minutes trending up.",
]

MINUTES_TEMPLATES = [
    "Projecting {player} for 34-36 minutes tonight in a pace-up spot for {team}.",
    "{player} minutes could dip tonight — blowout potential in this matchup.",
    "Love {player} in this spot. Projecting 30+ minutes with expanded usage for {team}.",
    "{player} averaging 28.5 MPG over last 10. Expect similar workload tonight.",
    "Minutes watch: {player} played 38 last game. Might see slight reduction tonight for {team}.",
]

TRADE_TEMPLATES = [
    "Keep an eye on {player}'s workload — {team} managing his minutes carefully post-deadline.",
    "{team} rotation still settling in after deadline moves. {player} adjusting to new role.",
    "Hearing {player} is fully committed to {team} for the playoff push.",
    "{team} front office happy with the roster. {player} is a key piece going forward.",
]

GENERAL_TEMPLATES = [
    "{player} has been absolutely cooking. Fantasy managers should be thrilled.",
    "Keep an eye on {player} tonight. {team} plays at a fast pace in this matchup.",
    "The {team} rotation has been in flux. Several guys seeing inconsistent minutes.",
    "{player} could be a sneaky DFS play tonight for {team}. Under-rostered.",
]

# Common NBA player names for mock data (updated Feb 2025 — post-trade-deadline)
MOCK_PLAYERS = {
    "ATL": ["Trae Young", "Jalen Johnson", "De'Andre Hunter", "Clint Capela", "Jonathan Kuminga"],
    "BOS": ["Jayson Tatum", "Jaylen Brown", "Derrick White", "Kristaps Porzingis", "Jrue Holiday"],
    "BKN": ["Cam Thomas", "D'Angelo Russell", "Nic Claxton", "Cam Johnson", "Dorian Finney-Smith"],
    "CHA": ["LaMelo Ball", "Brandon Miller", "Miles Bridges", "Mark Williams", "Tre Mann"],
    "CHI": ["Zach LaVine", "Coby White", "Nikola Vucevic", "Patrick Williams", "Ayo Dosunmu"],
    "CLE": ["Donovan Mitchell", "Darius Garland", "Evan Mobley", "Jarrett Allen", "Caris LeVert"],
    "DAL": ["Luka Doncic", "Kyrie Irving", "PJ Washington", "Daniel Gafford", "Klay Thompson"],
    "DEN": ["Nikola Jokic", "Jamal Murray", "Michael Porter Jr.", "Aaron Gordon", "Christian Braun"],
    "DET": ["Cade Cunningham", "Jaden Ivey", "Ausar Thompson", "Tobias Harris", "Jalen Duren"],
    "GSW": ["Stephen Curry", "Draymond Green", "Andrew Wiggins", "Buddy Hield", "Kevon Looney"],
    "HOU": ["Jalen Green", "Fred VanVleet", "Alperen Sengun", "Jabari Smith Jr.", "Dillon Brooks"],
    "IND": ["Tyrese Haliburton", "Pascal Siakam", "Myles Turner", "Bennedict Mathurin", "Andrew Nembhard"],
    "LAC": ["James Harden", "Kawhi Leonard", "Norman Powell", "Ivica Zubac", "Terance Mann"],
    "LAL": ["LeBron James", "Anthony Davis", "Austin Reaves", "Rui Hachimura", "Dorian Finney-Smith"],
    "MEM": ["Ja Morant", "Desmond Bane", "Jaren Jackson Jr.", "Marcus Smart", "Santi Aldama"],
    "MIA": ["Bam Adebayo", "Tyler Herro", "Terry Rozier", "Jaime Jaquez Jr.", "Nikola Jovic"],
    "MIL": ["Giannis Antetokounmpo", "Damian Lillard", "Khris Middleton", "Brook Lopez", "Bobby Portis"],
    "MIN": ["Anthony Edwards", "Julius Randle", "Rudy Gobert", "Jaden McDaniels", "Mike Conley"],
    "NOP": ["Zion Williamson", "Brandon Ingram", "CJ McCollum", "Trey Murphy III", "Herbert Jones"],
    "NYK": ["Jalen Brunson", "Karl-Anthony Towns", "Mikal Bridges", "OG Anunoby", "Josh Hart"],
    "OKC": ["Shai Gilgeous-Alexander", "Jalen Williams", "Chet Holmgren", "Lu Dort", "Isaiah Hartenstein"],
    "ORL": ["Paolo Banchero", "Franz Wagner", "Jalen Suggs", "Wendell Carter Jr.", "Cole Anthony"],
    "PHI": ["Joel Embiid", "Tyrese Maxey", "Paul George", "Caleb Martin", "Kelly Oubre Jr."],
    "PHX": ["Kevin Durant", "Devin Booker", "Bradley Beal", "Jusuf Nurkic", "Grayson Allen"],
    "POR": ["Anfernee Simons", "Scoot Henderson", "Jerami Grant", "Deandre Ayton", "Deni Avdija"],
    "SAC": ["De'Aaron Fox", "Domantas Sabonis", "Keegan Murray", "DeMar DeRozan", "Malik Monk"],
    "SAS": ["Victor Wembanyama", "Devin Vassell", "Jeremy Sochan", "Keldon Johnson", "Tre Jones"],
    "TOR": ["Scottie Barnes", "RJ Barrett", "Immanuel Quickley", "Jakob Poeltl", "Gradey Dick"],
    "UTA": ["Lauri Markkanen", "Collin Sexton", "Jordan Clarkson", "John Collins", "Walker Kessler"],
    "WAS": ["Jordan Poole", "Kyle Kuzma", "Bilal Coulibaly", "Alex Sarr", "Malcolm Brogdon"],
}

ALL_TEAMS = list(MOCK_PLAYERS.keys())

SIGNAL_TYPE_TEMPLATES = {
    "injury_update": INJURY_TEMPLATES,
    "rotation_change": ROTATION_TEMPLATES,
    "minutes_projection": MINUTES_TEMPLATES,
    "trade_rumor": TRADE_TEMPLATES,
    "general_take": GENERAL_TEMPLATES,
}

SENTIMENTS_BY_TYPE = {
    "injury_update": ["bearish", "bearish", "bullish", "bearish", "neutral"],
    "rotation_change": ["bullish", "neutral", "bearish", "bullish", "bullish"],
    "minutes_projection": ["bullish", "bearish", "bullish", "neutral", "neutral"],
    "trade_rumor": ["neutral", "bearish", "neutral", "neutral"],
    "general_take": ["bullish", "bullish", "neutral", "bullish"],
}


def generate_mock_signals(
    team_abbr: Optional[str] = None,
    player_names: Optional[List[str]] = None,
    count: int = 20,
) -> List[ExpertSignal]:
    """Generate realistic mock expert signals for development."""
    signals: List[ExpertSignal] = []
    now = datetime.now(timezone.utc)

    # Use provided team or pick random ones
    if team_abbr and team_abbr.upper() in MOCK_PLAYERS:
        target_teams = [team_abbr.upper()]
    elif team_abbr:
        # Unrecognised abbreviation — include all teams so we still
        # generate signals (some may randomly reference the right players)
        target_teams = ALL_TEAMS
    else:
        target_teams = ALL_TEAMS

    target_players = player_names or []

    # Build pool of players to mention
    player_pool = []
    for t in target_teams:
        if t in MOCK_PLAYERS:
            for p in MOCK_PLAYERS[t]:
                player_pool.append((p, t))

    if not player_pool:
        # Fallback: use all
        for t, players in MOCK_PLAYERS.items():
            for p in players:
                player_pool.append((p, t))

    twitter_experts = [e for e in EXPERT_REGISTRY if e.platform == "twitter"]

    for i in range(count):
        player, team = random.choice(player_pool)

        # Filter: if specific players requested, bias towards them
        if target_players and random.random() < 0.7:
            match = None
            for tp in target_players:
                for pp, pt in player_pool:
                    if tp.lower() in pp.lower():
                        match = (pp, pt)
                        break
                if match:
                    break
            if match:
                player, team = match

        signal_type = random.choice(list(SIGNAL_TYPE_TEMPLATES.keys()))
        templates = SIGNAL_TYPE_TEMPLATES[signal_type]
        template = random.choice(templates)
        content = template.format(player=player, team=team)

        sentiments = SENTIMENTS_BY_TYPE[signal_type]
        sentiment = sentiments[i % len(sentiments)]

        expert = random.choice(twitter_experts)

        # Random timestamp within last 24 hours, weighted toward recent
        hours_ago = random.expovariate(0.3)  # Exponential — most signals recent
        hours_ago = min(hours_ago, 24)
        ts = now - timedelta(hours=hours_ago)

        # Engagement (higher tier = more engagement)
        tier_multiplier = {ExpertTier.TIER_1: 50, ExpertTier.TIER_2: 10, ExpertTier.TIER_3: 2}
        base = tier_multiplier.get(expert.tier, 5)
        likes = random.randint(base, base * 20)
        rts = random.randint(base // 2, base * 5)

        # Relevance score
        tier_scores = {ExpertTier.TIER_1: 1.0, ExpertTier.TIER_2: 0.7, ExpertTier.TIER_3: 0.4}
        score = tier_scores.get(expert.tier, 0.5)
        if hours_ago < 1:
            score += 0.3
        elif hours_ago < 3:
            score += 0.2
        elif hours_ago < 6:
            score += 0.1
        if signal_type in ("rotation_change", "injury_update", "minutes_projection"):
            score += 0.2
        score = min(score, 2.0)

        sig_id = hashlib.md5(f"{expert.handle}|{content}|{i}".encode()).hexdigest()[:12]

        signals.append(ExpertSignal(
            id=sig_id,
            expert_name=expert.name,
            expert_handle=expert.handle,
            expert_tier=expert.tier,
            expert_specialty=expert.specialty,
            content=content,
            timestamp=ts,
            source_platform="twitter",
            source_url=f"https://twitter.com/{expert.handle}/status/{random.randint(10**17, 10**18)}",
            signal_type=signal_type,
            sentiment=sentiment,
            mentioned_players=[player],
            mentioned_team=team,
            relevance_score=round(score, 2),
            engagement={"likes": likes, "retweets": rts},
        ))

    # Sort by relevance descending
    signals.sort(key=lambda s: s.relevance_score, reverse=True)
    return signals[:count]
