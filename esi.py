"""ESI client with on-disk caching and error-limit handling.

No authentication is needed for the endpoints used here. OAuth (for skill
import) can be layered on later by extending ESIClient with a token provider.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

ESI_BASE = "https://esi.evetech.net/latest"
USER_AGENT = "eve-t1-calc/1.0 (local industry tool; malefic89@gmail.com)"

THE_FORGE = 10000002
JITA_44 = 60003760

CACHE_DIR = Path(__file__).parent / "data" / "cache"

ORDERS_TTL = 15 * 60          # 15 minutes
HISTORY_TTL = 24 * 60 * 60    # 24 hours
INDICES_TTL = 60 * 60         # 1 hour
PRICES_TTL = 60 * 60          # 1 hour


class ESIError(Exception):
    pass


class ESIClient:
    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._lock = threading.Lock()
        self._error_remain = 100
        self._error_reset_at = 0.0

    # ---------- low level ----------

    def _respect_error_limit(self) -> None:
        with self._lock:
            if self._error_remain <= 5:
                wait = max(0.0, self._error_reset_at - time.time())
                if wait > 0:
                    log.warning("ESI error budget low, sleeping %.0fs", wait)
                    time.sleep(wait)
                self._error_remain = 100

    def _note_headers(self, resp: requests.Response) -> None:
        remain = resp.headers.get("X-ESI-Error-Limit-Remain")
        reset = resp.headers.get("X-ESI-Error-Limit-Reset")
        if remain is not None:
            with self._lock:
                self._error_remain = int(remain)
                if reset is not None:
                    self._error_reset_at = time.time() + int(reset)

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        self._respect_error_limit()
        for attempt in range(4):
            try:
                resp = self.session.get(ESI_BASE + path, params=params, timeout=30)
            except requests.RequestException as e:
                if attempt == 3:
                    raise ESIError(f"network error on {path}: {e}") from e
                time.sleep(2 ** attempt)
                continue
            self._note_headers(resp)
            if resp.status_code in (502, 503, 504, 420):
                time.sleep(2 ** attempt + 1)
                continue
            if 400 <= resp.status_code < 500:
                # e.g. history returns 400 for type_ids that never trade
                raise ESIError(f"{resp.status_code} for {path}")
            resp.raise_for_status()
            return resp
        raise ESIError(f"ESI kept failing for {path}")

    # ---------- cache ----------

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _cache_get(self, key: str, ttl: int):
        p = self._cache_path(key)
        if p.exists() and time.time() - p.stat().st_mtime < ttl:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def _cache_put(self, key: str, data) -> None:
        p = self._cache_path(key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(p)

    def cache_age(self, key: str) -> float | None:
        p = self._cache_path(key)
        return time.time() - p.stat().st_mtime if p.exists() else None

    # ---------- endpoints ----------

    def jita_orders(self, force: bool = False, progress_cb=None) -> dict:
        """All Jita 4-4 orders in The Forge, grouped per type.

        Returns {type_id: {"buy": [[price, volume], ...] desc,
                           "sell": [[price, volume], ...] asc}}
        (JSON round-trips turn int keys into str; callers normalize.)
        """
        key = f"orders_{THE_FORGE}"
        if not force:
            cached = self._cache_get(key, ORDERS_TTL)
            if cached is not None:
                return cached

        path = f"/markets/{THE_FORGE}/orders/"
        first = self._get(path, {"order_type": "all", "page": 1})
        pages = int(first.headers.get("X-Pages", 1))
        all_orders = first.json()
        for page in range(2, pages + 1):
            all_orders.extend(self._get(path, {"order_type": "all", "page": page}).json())
            if progress_cb:
                progress_cb(page, pages)

        book: dict[int, dict[str, list]] = {}
        for o in all_orders:
            if o["location_id"] != JITA_44:
                continue
            side = "buy" if o["is_buy_order"] else "sell"
            book.setdefault(o["type_id"], {"buy": [], "sell": []})[side].append(
                [o["price"], o["volume_remain"]]
            )
        for b in book.values():
            b["buy"].sort(key=lambda x: -x[0])
            b["sell"].sort(key=lambda x: x[0])

        self._cache_put(key, book)
        return book

    def history(self, type_id: int, force: bool = False) -> list[dict]:
        key = f"history_{type_id}"
        if not force:
            cached = self._cache_get(key, HISTORY_TTL)
            if cached is not None:
                return cached
        try:
            data = self._get(f"/markets/{THE_FORGE}/history/", {"type_id": type_id}).json()
        except ESIError:
            data = []
        self._cache_put(key, data)
        return data

    def system_cost_indices(self, force: bool = False) -> dict[int, dict[str, float]]:
        """{system_id: {activity_name: cost_index}}"""
        key = "cost_indices"
        cached = None if force else self._cache_get(key, INDICES_TTL)
        if cached is None:
            raw = self._get("/industry/systems/").json()
            cached = {
                str(s["solar_system_id"]): {
                    ci["activity"]: ci["cost_index"] for ci in s["cost_indices"]
                }
                for s in raw
            }
            self._cache_put(key, cached)
        return {int(k): v for k, v in cached.items()}

    def adjusted_prices(self, force: bool = False) -> dict[int, float]:
        """{type_id: adjusted_price}"""
        key = "adjusted_prices"
        cached = None if force else self._cache_get(key, PRICES_TTL)
        if cached is None:
            raw = self._get("/markets/prices/").json()
            cached = {str(p["type_id"]): p.get("adjusted_price", 0.0) for p in raw}
            self._cache_put(key, cached)
        return {int(k): v for k, v in cached.items()}
