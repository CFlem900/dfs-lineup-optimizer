"""Tests for PlayerIdMapper — DK ↔ BDL name mapping with fuzzy matching."""

import pytest

from app.services.player_id_mapper import (
    DKPlayerInput,
    PlayerIdMapper,
    PlayerMapping,
    normalize_name,
)


# ── normalize_name tests ─────────────────────────────────────────────────


class TestNormalizeName:
    def test_basic(self):
        assert normalize_name("LeBron James") == "lebron james"

    def test_suffix_jr(self):
        assert normalize_name("Gary Trent Jr.") == "gary trent"

    def test_suffix_iii(self):
        assert normalize_name("Robert Williams III") == "robert williams"

    def test_suffix_ii(self):
        assert normalize_name("Dereck Lively II") == "dereck lively"

    def test_periods(self):
        assert normalize_name("P.J. Washington") == "pj washington"

    def test_accented(self):
        assert normalize_name("Nikola Jokić") == "nikola jokic"

    def test_extra_whitespace(self):
        assert normalize_name("  Shai   Gilgeous-Alexander  ") == "shai gilgeous alexander"

    def test_sr_suffix(self):
        assert normalize_name("Marcus Morris Sr.") == "marcus morris"


# ── Mock services ─────────────────────────────────────────────────────────


def _make_bdl_player(pid: int, first: str, last: str, team_abbr: str = "BKN") -> dict:
    return {
        "id": pid,
        "first_name": first,
        "last_name": last,
        "position": "F",
        "team": {"id": 3, "abbreviation": team_abbr},
    }


# BDL rosters by team abbreviation
MOCK_ROSTERS = {
    "BKN": [
        _make_bdl_player(100, "Nicolas", "Claxton", "BKN"),
        _make_bdl_player(101, "Cam", "Thomas", "BKN"),
        _make_bdl_player(102, "Mikal", "Bridges", "BKN"),
        _make_bdl_player(103, "Ben", "Simmons", "BKN"),
    ],
    "DAL": [
        _make_bdl_player(200, "Luka", "Doncic", "DAL"),
        _make_bdl_player(201, "Kyrie", "Irving", "DAL"),
        _make_bdl_player(202, "Dereck", "Lively II", "DAL"),
    ],
    "OKC": [
        _make_bdl_player(300, "Shai", "Gilgeous-Alexander", "OKC"),
        _make_bdl_player(301, "Jalen", "Williams", "OKC"),
        _make_bdl_player(302, "Chet", "Holmgren", "OKC"),
    ],
}


class MockBDLService:
    """Minimal mock of BallDontLieService for testing."""

    def __init__(self):
        self._bdl_team_abbrs = {
            3: "BKN",
            7: "DAL",
            21: "OKC",
        }
        self._teams_loaded = True

    def _ensure_team_mapping(self):
        pass

    def _get(self, path: str) -> dict:
        # Parse team ID from path like /players?team_ids[]=3&per_page=100
        for bdl_id, abbr in self._bdl_team_abbrs.items():
            if f"team_ids[]={bdl_id}" in path:
                return {"data": MOCK_ROSTERS.get(abbr, [])}
        return {"data": []}


class MockCacheService:
    """In-memory mock of CacheService for testing."""

    def __init__(self):
        self._store: dict = {}

    async def get(self, key: str):
        return self._store.get(key)

    async def set(self, key: str, value, ttl=None):
        self._store[key] = value

    async def delete(self, key: str):
        self._store.pop(key, None)

    async def clear_pattern(self, pattern: str):
        import fnmatch
        keys = [k for k in self._store if fnmatch.fnmatch(k, pattern)]
        for k in keys:
            del self._store[k]


@pytest.fixture
def mapper():
    return PlayerIdMapper(MockBDLService(), MockCacheService())


@pytest.fixture
def mapper_no_cache():
    return PlayerIdMapper(MockBDLService(), None)


# ── Exact match tests ─────────────────────────────────────────────────────


class TestExactMatch:
    @pytest.mark.asyncio
    async def test_exact_name(self, mapper):
        pm = await mapper.resolve("Luka Doncic", "DAL")
        assert pm is not None
        assert pm.bdl_id == 200
        assert pm.match_method == "exact"
        assert pm.confidence == 1.0

    @pytest.mark.asyncio
    async def test_case_insensitive(self, mapper):
        pm = await mapper.resolve("MIKAL BRIDGES", "BKN")
        assert pm is not None
        assert pm.bdl_id == 102

    @pytest.mark.asyncio
    async def test_suffix_stripped(self, mapper):
        """DK says 'Dereck Lively II', BDL says 'Dereck Lively II' — both normalize."""
        pm = await mapper.resolve("Dereck Lively II", "DAL")
        assert pm is not None
        assert pm.bdl_id == 202


class TestAliasMatch:
    @pytest.mark.asyncio
    async def test_nick_to_nicolas(self, mapper):
        """'Nick Claxton' (DK) → 'Nicolas Claxton' (BDL) via alias table."""
        pm = await mapper.resolve("Nick Claxton", "BKN")
        assert pm is not None
        assert pm.bdl_id == 100
        assert pm.match_method == "alias"


class TestLastNameMatch:
    @pytest.mark.asyncio
    async def test_unique_last_name(self, mapper):
        """When first name differs but last name is unique on the team."""
        pm = await mapper.resolve("K. Irving", "DAL")
        # normalize_name("K. Irving") → "k irving"
        # No exact match, no alias, but last name "irving" is unique on DAL
        assert pm is not None
        assert pm.bdl_id == 201
        assert pm.match_method == "last_name"

    @pytest.mark.asyncio
    async def test_ambiguous_last_name_skipped(self, mapper):
        """If two players share a last name on the same team, skip."""
        # Add a second Williams to OKC
        MOCK_ROSTERS["OKC"].append(
            _make_bdl_player(303, "Mark", "Williams", "OKC")
        )
        mapper._roster_index.clear()  # Force re-index
        pm = await mapper.resolve("J. Williams", "OKC")
        # Last-name match should NOT fire (two Williams on OKC)
        # But fuzzy might still match
        if pm:
            assert pm.match_method != "last_name"
        # Clean up
        MOCK_ROSTERS["OKC"].pop()


class TestFuzzyMatch:
    @pytest.mark.asyncio
    async def test_fuzzy_shai(self, mapper):
        """'Shai Gilgeous Alexander' (no hyphen) → 'Shai Gilgeous-Alexander'."""
        # This specific case goes through alias first, but test the concept
        pm = await mapper.resolve("Shai Gilgeous Alexander", "OKC")
        assert pm is not None
        assert pm.bdl_id == 300

    @pytest.mark.asyncio
    async def test_no_match_wrong_team(self, mapper):
        """Player exists but wrong team → no match."""
        pm = await mapper.resolve("Luka Doncic", "BKN")
        assert pm is None


# ── Caching tests ─────────────────────────────────────────────────────────


class TestCaching:
    @pytest.mark.asyncio
    async def test_result_cached(self, mapper):
        pm1 = await mapper.resolve("Luka Doncic", "DAL")
        assert pm1 is not None

        # Second call should come from cache
        pm2 = await mapper.resolve("Luka Doncic", "DAL")
        assert pm2 is not None
        assert pm2.match_method == "cached"
        assert pm2.bdl_id == pm1.bdl_id

    @pytest.mark.asyncio
    async def test_invalidate(self, mapper):
        await mapper.resolve("Luka Doncic", "DAL")
        await mapper.invalidate("Luka Doncic", "DAL")
        # Next call should re-resolve (not cached)
        pm = await mapper.resolve("Luka Doncic", "DAL")
        assert pm is not None
        assert pm.match_method == "exact"

    @pytest.mark.asyncio
    async def test_no_cache_still_works(self, mapper_no_cache):
        pm = await mapper_no_cache.resolve("Luka Doncic", "DAL")
        assert pm is not None
        assert pm.bdl_id == 200


# ── build_mapping tests ──────────────────────────────────────────────────


class TestBuildMapping:
    @pytest.mark.asyncio
    async def test_batch_mapping(self, mapper):
        dk_players = [
            DKPlayerInput(name="Luka Doncic", team="DAL"),
            DKPlayerInput(name="Mikal Bridges", team="BKN"),
            DKPlayerInput(name="Shai Gilgeous-Alexander", team="OKC"),
        ]
        result = await mapper.build_mapping(dk_players)
        assert len(result) == 3
        assert result["Luka Doncic"].bdl_id == 200
        assert result["Mikal Bridges"].bdl_id == 102
        assert result["Shai Gilgeous-Alexander"].bdl_id == 300

    @pytest.mark.asyncio
    async def test_unresolvable_player_omitted(self, mapper):
        dk_players = [
            DKPlayerInput(name="Luka Doncic", team="DAL"),
            DKPlayerInput(name="Fake Player", team="DAL"),
        ]
        result = await mapper.build_mapping(dk_players)
        assert "Luka Doncic" in result
        assert "Fake Player" not in result
