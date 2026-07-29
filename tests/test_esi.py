"""ESI client behaviour.

Guards the project privacy rule: the committed default User-Agent must carry no
personal data. CCP asks for a contact, and it comes from EVE_CALC_CONTACT at
runtime instead of being hardcoded — see the privacy rule in CLAUDE.md.
"""
import esi


def test_user_agent_carries_no_personal_data():
    import importlib
    import os

    saved = os.environ.pop("EVE_CALC_CONTACT", None)
    try:
        fresh = importlib.reload(esi)
        assert "@" not in fresh.USER_AGENT
        assert fresh.ESI_CONTACT == ""

        os.environ["EVE_CALC_CONTACT"] = "someone@example.com"
        fresh = importlib.reload(esi)
        assert "someone@example.com" in fresh.USER_AGENT
    finally:
        os.environ.pop("EVE_CALC_CONTACT", None)
        if saved is not None:
            os.environ["EVE_CALC_CONTACT"] = saved
        importlib.reload(esi)
