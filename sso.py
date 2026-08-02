"""EVE SSO (PKCE) plus the mapping from character data onto Settings.

The native/PKCE flow is deliberate: it has **no client secret**, so nothing in
this project can leak one. The client id is public by design. The refresh token
is not — it lives in `data/sso.json` (gitignored), must never be logged and must
never appear in an API response.

Everything above the I/O line here is pure and unit-tested; the network calls are
thin wrappers. Character data is never written to the ESI disk cache.

Flow reference: https://docs.esi.evetech.net/docs/sso/native_sso_flow.html
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlencode

import requests

AUTHORIZE_URL = "https://login.eveonline.com/v2/oauth/authorize/"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"

# Variant A imports skills and standings. Ask for nothing more: unused scopes are
# consent the user has no reason to grant.
SCOPES = (
    "esi-skills.read_skills.v1",
    "esi-characters.read_standings.v1",
)

DATA_DIR = Path(__file__).parent / "data"
STATE_FILE = DATA_DIR / "sso.json"

# skill name in the SDE -> Settings field. Names, not IDs: sde.type_id_by_name
# resolves them, so a renamed or re-IDed skill surfaces as a clear error.
SKILL_FIELDS = {
    "Accounting": "accounting",
    "Broker Relations": "broker_relations",
    "Industry": "industry",
    "Advanced Industry": "advanced_industry",
}


class SSOError(Exception):
    pass


# ---------- pure: PKCE ----------

def _b64url(raw: bytes) -> str:
    """Base64url without padding, as the OAuth PKCE spec requires."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) for one login attempt.

    32 random bytes per the docs; the challenge is the SHA-256 of the *encoded*
    verifier string, not of the raw bytes — getting that wrong yields an
    `invalid_grant` at token exchange that looks like a server problem.
    """
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def authorize_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })


# ---------- pure: token payload ----------

def character_from_access_token(access_token: str) -> tuple[int, str]:
    """(character_id, character_name) out of the access token's JWT claims.

    The signature is not verified, and that is safe here for one specific reason:
    this token came straight back from the SSO token endpoint over TLS in the
    same request. We never accept a token from anywhere else, so there is no
    attacker in a position to forge one. (Verifying would mean fetching and
    caching CCP's JWKS — worth it for a service that accepts third-party tokens,
    pointless for this.)
    """
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)          # JWTs drop the padding
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception as e:
        raise SSOError(f"cannot read the access token: {e}") from e
    sub = str(claims.get("sub", ""))                   # "CHARACTER:EVE:12345"
    if not sub.startswith("CHARACTER:EVE:"):
        raise SSOError(f"unexpected token subject {sub!r}")
    return int(sub.rsplit(":", 1)[1]), str(claims.get("name", ""))


# ---------- pure: character data -> settings ----------

def skills_to_settings(payload: dict, skill_ids: dict[str, int]) -> dict[str, int]:
    """{Settings field: level} from an ESI /skills/ payload.

    Reads `active_skill_level`, not `trained_skill_level`: an alpha clone has
    trained levels its clone state will not let it use, and the calculator has to
    model what is actually in effect.

    A skill missing from the payload is untrained, so it maps to 0 rather than
    being left out — otherwise importing would silently keep a stale manual value
    for a skill the character does not have.
    """
    levels = {
        s.get("skill_id"): s.get("active_skill_level", 0)
        for s in (payload.get("skills") or [])
    }
    out: dict[str, int] = {}
    for name, field in SKILL_FIELDS.items():
        tid = skill_ids.get(name)
        if tid is None:
            raise SSOError(f"skill {name!r} not found in the SDE")
        out[field] = int(levels.get(tid) or 0)
    return out


def standings_for(payload, corporation_id: int, faction_id: int) -> dict[str, float]:
    """Standings toward the trade hub's owning corp and faction.

    ESI lists only entities the character has a recorded standing with; anything
    absent is 0.0, which is the same as neutral for the broker fee.

    ESI reports **base** standings — which is what the broker fee needs, since
    Connections and friends must not count — and at full precision. Settled by
    measurement, not by the docs: the imported 7.892620134 / 9.444335456
    reproduce a real 24,881.59 ISK broker fee to 0.0013 ISK, where the same pair
    rounded to the hundredths the client displays misses by 3.83, and an
    effective (Connections-inflated) value could not reconcile at all. So an
    import is strictly more accurate than typing the numbers off the character
    sheet, and needs no reverse correction.
    """
    by: dict[tuple, float] = {}
    for e in payload or []:
        by[(e.get("from_type"), e.get("from_id"))] = float(e.get("standing") or 0.0)
    return {
        "faction_standing": by.get(("faction", faction_id), 0.0),
        "corp_standing": by.get(("npc_corp", corporation_id), 0.0),
    }


# ---------- token store ----------

@dataclass
class SSOState:
    client_id: str = ""
    refresh_token: str = ""        # secret
    character_id: int = 0
    character_name: str = ""
    imported_at: str = ""

    @property
    def connected(self) -> bool:
        return bool(self.refresh_token and self.character_id)

    def public(self) -> dict:
        """Everything the frontend may see. Never includes the refresh token."""
        return {
            "client_id_set": bool(self.client_id),
            "connected": self.connected,
            "character_id": self.character_id,
            "character_name": self.character_name,
            "imported_at": self.imported_at,
            "scopes": list(SCOPES),
        }


def load_state() -> SSOState:
    """Stored state, with EVE_CALC_SSO_CLIENT_ID overriding the saved client id."""
    data = {}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    known = set(SSOState.__dataclass_fields__)
    s = SSOState(**{k: v for k, v in data.items() if k in known})
    env = os.environ.get("EVE_CALC_SSO_CLIENT_ID", "").strip()
    if env:
        s.client_id = env
    return s


def save_state(s: SSOState) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s), indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------- network ----------

def _post_token(form: dict, user_agent: str) -> dict:
    """Token endpoint call. Since 2025 it accepts form encoding only — not JSON,
    not query parameters."""
    try:
        r = requests.post(
            TOKEN_URL, data=form, timeout=30,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": user_agent},
        )
    except requests.RequestException as e:
        raise SSOError(f"cannot reach the EVE SSO: {e}") from e
    if r.status_code != 200:
        # the body names the actual problem (invalid_grant, invalid_client, ...)
        raise SSOError(f"SSO returned {r.status_code}: {r.text[:200]}")
    return r.json()


def exchange_code(client_id: str, code: str, verifier: str, user_agent: str) -> dict:
    return _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "code_verifier": verifier,
    }, user_agent)


def refresh_tokens(client_id: str, refresh_token: str, user_agent: str) -> dict:
    """New access token. The response may carry a *different* refresh token —
    CCP is rolling out rotation for native apps — so callers must store whatever
    comes back instead of assuming the old one stays valid."""
    return _post_token({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }, user_agent)
