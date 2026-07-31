"""SSO helpers and the character-data -> Settings mapping.

All of this is the pure half of sso.py, so it runs without a network or a token.
"""
import base64
import hashlib
import json

import pytest

import sso


class TestPkce:
    def test_challenge_is_sha256_of_the_encoded_verifier(self):
        """The challenge hashes the base64url *string*, not the raw bytes it came
        from. Hashing the bytes instead yields an invalid_grant at token exchange
        that reads like a server fault."""
        verifier, challenge = sso.pkce_pair()
        expect = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode().rstrip("=")
        assert challenge == expect

    def test_no_padding_and_url_safe(self):
        for value in sso.pkce_pair():
            assert "=" not in value and "+" not in value and "/" not in value

    def test_each_attempt_is_fresh(self):
        assert sso.pkce_pair()[0] != sso.pkce_pair()[0]


class TestAuthorizeUrl:
    def test_carries_every_required_parameter(self):
        url = sso.authorize_url("cid", "http://127.0.0.1:8000/api/sso/callback", "chal", "st")
        assert url.startswith(sso.AUTHORIZE_URL + "?")
        for part in ("response_type=code", "client_id=cid", "code_challenge=chal",
                     "code_challenge_method=S256", "state=st"):
            assert part in url
        # redirect_uri and the space-delimited scopes must be percent-encoded
        assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Fapi%2Fsso%2Fcallback" in url
        assert "esi-skills.read_skills.v1+esi-characters.read_standings.v1" in url

    def test_asks_for_nothing_beyond_skills_and_standings(self):
        assert set(sso.SCOPES) == {
            "esi-skills.read_skills.v1", "esi-characters.read_standings.v1"}


def _token(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "header." + body + ".signature"


class TestCharacterFromToken:
    def test_reads_id_and_name(self):
        t = _token({"sub": "CHARACTER:EVE:2117538509", "name": "Some Pilot"})
        assert sso.character_from_access_token(t) == (2117538509, "Some Pilot")

    def test_rejects_a_non_character_subject(self):
        # a corporation token would authorise nothing we ask for
        with pytest.raises(sso.SSOError):
            sso.character_from_access_token(_token({"sub": "CORPORATION:EVE:1"}))

    def test_rejects_garbage(self):
        with pytest.raises(sso.SSOError):
            sso.character_from_access_token("not-a-jwt")


SKILL_IDS = {"Accounting": 16622, "Broker Relations": 3446,
             "Industry": 3380, "Advanced Industry": 3388}


class TestSkillsToSettings:
    def test_maps_all_four(self):
        payload = {"skills": [
            {"skill_id": 16622, "active_skill_level": 5},
            {"skill_id": 3446, "active_skill_level": 4},
            {"skill_id": 3380, "active_skill_level": 5},
            {"skill_id": 3388, "active_skill_level": 3},
        ]}
        assert sso.skills_to_settings(payload, SKILL_IDS) == {
            "accounting": 5, "broker_relations": 4,
            "industry": 5, "advanced_industry": 3}

    def test_missing_skill_becomes_zero_not_omitted(self):
        """An untrained skill has to overwrite the manual value; leaving the key
        out would silently keep whatever was typed before."""
        out = sso.skills_to_settings({"skills": [
            {"skill_id": 16622, "active_skill_level": 2}]}, SKILL_IDS)
        assert out == {"accounting": 2, "broker_relations": 0,
                       "industry": 0, "advanced_industry": 0}

    def test_prefers_active_over_trained_level(self):
        """Alpha clones carry trained levels they cannot use; the calculation must
        follow what is in effect."""
        out = sso.skills_to_settings({"skills": [
            {"skill_id": 3380, "active_skill_level": 2, "trained_skill_level": 5}]},
            SKILL_IDS)
        assert out["industry"] == 2

    def test_empty_and_absent_payloads(self):
        for payload in ({}, {"skills": None}, {"skills": []}):
            assert sso.skills_to_settings(payload, SKILL_IDS) == {
                "accounting": 0, "broker_relations": 0,
                "industry": 0, "advanced_industry": 0}

    def test_unknown_skill_name_is_an_error_not_a_zero(self):
        with pytest.raises(sso.SSOError):
            sso.skills_to_settings({"skills": []}, {"Accounting": 16622})


class TestStandingsFor:
    CORP, FACTION = 1000035, 500001          # Caldari Navy, Caldari State

    def test_picks_the_hub_owner_out_of_the_list(self):
        payload = [
            {"from_type": "faction", "from_id": 500001, "standing": 8.31},
            {"from_type": "npc_corp", "from_id": 1000035, "standing": 9.56},
            {"from_type": "faction", "from_id": 500003, "standing": -2.0},
            {"from_type": "agent", "from_id": 3019494, "standing": 5.0},
        ]
        assert sso.standings_for(payload, self.CORP, self.FACTION) == {
            "faction_standing": 8.31, "corp_standing": 9.56}

    def test_absent_entity_is_neutral(self):
        """ESI lists only recorded standings, and no record means 0 for the fee."""
        assert sso.standings_for([], self.CORP, self.FACTION) == {
            "faction_standing": 0.0, "corp_standing": 0.0}

    def test_does_not_confuse_a_corp_id_with_a_faction_id(self):
        payload = [{"from_type": "npc_corp", "from_id": 500001, "standing": 7.0}]
        assert sso.standings_for(payload, self.CORP, self.FACTION)["faction_standing"] == 0.0

    def test_negative_standing_passes_through(self):
        payload = [{"from_type": "faction", "from_id": 500001, "standing": -4.5}]
        assert sso.standings_for(payload, self.CORP, self.FACTION)["faction_standing"] == -4.5


class TestStateStore:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sso, "DATA_DIR", tmp_path)
        monkeypatch.setattr(sso, "STATE_FILE", tmp_path / "sso.json")
        monkeypatch.delenv("EVE_CALC_SSO_CLIENT_ID", raising=False)

    def test_round_trip(self):
        sso.save_state(sso.SSOState(client_id="abc", refresh_token="rt",
                                    character_id=7, character_name="Pilot"))
        got = sso.load_state()
        assert (got.client_id, got.refresh_token, got.character_id) == ("abc", "rt", 7)
        assert got.connected

    def test_missing_file_is_a_blank_state(self):
        st = sso.load_state()
        assert not st.connected and st.client_id == ""

    def test_corrupt_file_does_not_crash_startup(self):
        sso.STATE_FILE.write_text("{ not json", encoding="utf-8")
        assert not sso.load_state().connected

    def test_public_never_leaks_the_refresh_token(self):
        st = sso.SSOState(client_id="abc", refresh_token="SECRET", character_id=7)
        assert "SECRET" not in repr(st.public())
        assert "refresh_token" not in st.public()
        assert st.public()["client_id_set"] is True

    def test_env_var_overrides_the_stored_client_id(self, monkeypatch):
        sso.save_state(sso.SSOState(client_id="from-file"))
        monkeypatch.setenv("EVE_CALC_SSO_CLIENT_ID", "from-env")
        assert sso.load_state().client_id == "from-env"

    def test_connected_needs_both_token_and_character(self):
        assert not sso.SSOState(refresh_token="rt").connected
        assert not sso.SSOState(character_id=7).connected
