"""ESI client caching and error handling.

The distinction that matters here: a 4xx means "this type never trades" and is
worth remembering for the full TTL, while a network failure means we learned
nothing and must not be cached as an empty history.
"""
import threading

import requests

import esi


def _order(type_id, price, buy=False, volume=7, location=None):
    return {
        "type_id": type_id,
        "is_buy_order": buy,
        "location_id": esi.JITA_44 if location is None else location,
        "price": price,
        "volume_remain": volume,
    }


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
        self.headers_seen = []          # to assert the Bearer on authed routes
        self._lock = threading.Lock()   # order pages are fetched concurrently

    def get(self, url, params=None, timeout=None, headers=None):
        with self._lock:
            self.calls += 1
            self.headers_seen.append(headers)
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


class _PagedResp(_Resp):
    def __init__(self, payload, pages=None):
        super().__init__(200, payload)
        if pages is not None:
            self.headers["X-Pages"] = str(pages)


def test_orders_fetched_in_parallel_across_all_pages(tmp_path, monkeypatch):
    """Every page must land in the book exactly once, whatever order the
    threads complete in, and progress must report each page."""
    pages = 5
    queue = [_PagedResp([_order(34, 10.0 + p)], pages=pages) for p in range(1, pages + 1)]
    c = _client(tmp_path, queue, monkeypatch)
    seen = []
    book = c.jita_orders(progress_cb=lambda done, total: seen.append((done, total)))

    assert c.session.calls == pages
    prices = sorted(o[0] for o in book[34]["sell"])
    assert prices == [11.0, 12.0, 13.0, 14.0, 15.0]
    assert sorted(seen) == [(p, pages) for p in range(2, pages + 1)]


def test_single_page_orders_need_no_workers(tmp_path, monkeypatch):
    c = _client(tmp_path, [_PagedResp([_order(34, 5.0)], pages=1)], monkeypatch)
    book = c.jita_orders()
    assert c.session.calls == 1
    assert book[34]["sell"] == [[5.0, 7]]


def test_failed_page_aborts_the_fetch(tmp_path, monkeypatch):
    """A partial book would price real materials as unpriceable and silently
    change every margin, so one bad page must fail the whole fetch."""
    queue = [_PagedResp([_order(34, 10.0)], pages=3), _Resp(404)]
    c = _client(tmp_path, queue, monkeypatch)
    try:
        c.jita_orders()
    except esi.ESIError:
        pass
    else:
        raise AssertionError("a failed page must not yield a partial book")
    assert not (tmp_path / f"orders_{esi.THE_FORGE}.json").exists()


def test_authenticated_routes_send_the_bearer_and_skip_the_cache(tmp_path, monkeypatch):
    """Character data is personal: it must go out with the token and must not be
    written to the on-disk cache the public endpoints share."""
    payload = {"skills": [{"skill_id": 3380, "active_skill_level": 5}]}
    c = _client(tmp_path, [_Resp(200, payload)], monkeypatch)
    assert c.character_skills(999, "tok-abc") == payload
    assert c.session.headers_seen[-1] == {"Authorization": "Bearer tok-abc"}
    assert list(tmp_path.glob("*.json")) == []


def test_public_routes_send_no_authorization_header(tmp_path, monkeypatch):
    c = _client(tmp_path, [_Resp(400)], monkeypatch)
    c.history(1234)
    assert c.session.headers_seen[-1] is None


def test_standings_route_is_authenticated_too(tmp_path, monkeypatch):
    rows = [{"from_type": "faction", "from_id": 500001, "standing": 8.31}]
    c = _client(tmp_path, [_Resp(200, rows)], monkeypatch)
    assert c.character_standings(999, "t") == rows
    assert c.session.headers_seen[-1] == {"Authorization": "Bearer t"}


def test_esi_error_carries_4xx_status(tmp_path, monkeypatch):
    c = _client(tmp_path, [_Resp(404)], monkeypatch)
    try:
        c._get("/whatever/")
    except esi.ESIError as e:
        assert e.status == 404
    else:
        raise AssertionError("4xx must raise")
