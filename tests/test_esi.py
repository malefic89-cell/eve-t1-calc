"""ESI client caching and error handling.

The distinction that matters here: a 4xx means "this type never trades" and is
worth remembering for the full TTL, while a network failure means we learned
nothing and must not be cached as an empty history.
"""
import requests

import esi


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.headers = {}
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class _Session:
    """Stands in for requests.Session; queued items are returned or raised."""

    def __init__(self, queue):
        self.queue = list(queue)
        self.headers = {}
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        item = self.queue.pop(0) if self.queue else _Resp(500)
        if isinstance(item, Exception):
            raise item
        return item


def _client(tmp_path, queue, monkeypatch):
    monkeypatch.setattr(esi.time, "sleep", lambda _s: None)  # no retry backoff
    c = esi.ESIClient(cache_dir=tmp_path)
    c.session = _Session(queue)
    return c


def test_4xx_history_is_empty_and_cached(tmp_path, monkeypatch):
    c = _client(tmp_path, [_Resp(400)], monkeypatch)
    assert c.history(1234) == []
    assert (tmp_path / "history_1234.json").exists()
    # served from cache on the second call, no further request
    assert c.history(1234) == []
    assert c.session.calls == 1


def test_network_failure_raises_and_leaves_no_cache(tmp_path, monkeypatch):
    err = requests.RequestException("connection reset")
    c = _client(tmp_path, [err] * 4, monkeypatch)
    try:
        c.history(1234)
    except esi.ESIError as e:
        assert e.status is None
    else:
        raise AssertionError("a transient failure must propagate, not return []")
    assert not (tmp_path / "history_1234.json").exists()


def test_network_failure_does_not_overwrite_good_cache(tmp_path, monkeypatch):
    good = [{"date": "2026-07-28", "volume": 500, "average": 10.0}]
    c = _client(tmp_path, [_Resp(200, good)], monkeypatch)
    assert c.history(1234) == good

    # same cache dir, expired TTL, and ESI now unreachable
    monkeypatch.setattr(esi, "HISTORY_TTL", 0)
    c2 = _client(tmp_path, [requests.RequestException("down")] * 4, monkeypatch)
    try:
        c2.history(1234)
    except esi.ESIError:
        pass
    monkeypatch.setattr(esi, "HISTORY_TTL", 24 * 3600)
    assert c2.history(1234) == good, "good history was clobbered by a failed refetch"


def test_persistent_5xx_is_transient_not_cached(tmp_path, monkeypatch):
    c = _client(tmp_path, [_Resp(503)] * 4, monkeypatch)
    try:
        c.history(1234)
    except esi.ESIError as e:
        assert e.status is None
    else:
        raise AssertionError("exhausted 5xx retries must not cache []")
    assert not (tmp_path / "history_1234.json").exists()


def test_user_agent_carries_no_personal_data():
    """Guards the project privacy rule: the committed default must be anonymous.
    The contact CCP asks for comes from EVE_CALC_CONTACT at runtime."""
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


def test_esi_error_carries_4xx_status(tmp_path, monkeypatch):
    c = _client(tmp_path, [_Resp(404)], monkeypatch)
    try:
        c._get("/whatever/")
    except esi.ESIError as e:
        assert e.status == 404
    else:
        raise AssertionError("4xx must raise")
