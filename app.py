"""FastAPI app: bootstrap (SDE download), price refresh, table & detail APIs.

Run: uvicorn app:app  (or `python app.py`)
"""
from __future__ import annotations

import concurrent.futures
import datetime
import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import calc
import config
import sde as sde_mod
import sso
from esi import JITA_44, USER_AGENT, ESIClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Bootstrap runs in a daemon thread so the server answers /api/status
    # (and serves the page) while the SDE and order book are still loading.
    threading.Thread(target=lambda: (bootstrap(), _after_bootstrap()), daemon=True).start()
    yield


app = FastAPI(title="EVE T1 Manufacturing Profit Calculator", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class State:
    """Global app state guarded by a lock; heavy work runs in threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "starting"          # starting | downloading_sde | loading | fetching_orders | ready | error
        self.progress = ""                # human-readable progress line
        self.error: str | None = None

        self.settings = config.load_settings()
        self.esi = ESIClient()
        self.sde: sde_mod.SDE | None = None
        self.products: list[sde_mod.Product] = []
        self.materials: dict[int, list[sde_mod.Material]] = {}   # blueprint_type_id -> materials
        self.book: dict[int, dict] = {}                          # type_id -> {"buy": [...], "sell": [...]}
        self.adjusted: dict[int, float] = {}
        self.cost_indices: dict[int, dict] = {}
        self.volumes: dict[int, float] = {}                      # type_id -> avg daily volume
        self.hist_prices: dict[int, dict] = {}                   # type_id -> {"p5": .., "p95": ..}
        self.bpo_sell_types: set[int] | None = None              # region-wide sell presence (None = unknown)
        self.rows: list[dict] = []

        self.history_total = 0
        self.history_done = 0
        self.history_running = False

        # One login attempt in flight: PKCE verifier + state, held in memory only
        # so a restart simply invalidates a half-finished login.
        self.sso_pending: dict | None = None


S = State()


# ---------------- bootstrap ----------------

def _set(status: str | None = None, progress: str | None = None, error: str | None = None):
    with S.lock:
        if status is not None:
            S.status = status
        if progress is not None:
            S.progress = progress
        if error is not None:
            S.error = error


def bootstrap():
    try:
        if not sde_mod.sde_exists():
            _set("downloading_sde", "Downloading SDE (~120 MB)...")

            def cb(stage, done, total):
                if stage == "download" and total:
                    _set(progress=f"Downloading SDE: {done // 2**20} / {total // 2**20} MB")
                elif stage == "decompress":
                    _set(progress=f"Decompressing SDE: {done // 2**20} MB written")

            sde_mod.download_sde(cb)

        _set("loading", "Loading blueprints from SDE...")
        S.sde = sde_mod.SDE()
        S.products = S.sde.manufacturable_t1_products()
        for p in S.products:
            if p.blueprint_type_id not in S.materials:
                S.materials[p.blueprint_type_id] = S.sde.materials_for_blueprint(p.blueprint_type_id)
        log.info("Loaded %d T1 products", len(S.products))

        refresh_prices(force=False)
    except Exception as e:
        log.exception("bootstrap failed")
        _set("error", error=str(e))


def refresh_prices(force: bool):
    _set("fetching_orders", "Fetching adjusted prices and cost indices...")
    adjusted = S.esi.adjusted_prices(force=force)
    indices = S.esi.system_cost_indices(force=force)

    _set(progress="Fetching Jita market orders (this can take a minute)...")

    def cb(page, pages):
        _set(progress=f"Fetching Jita orders: page {page}/{pages}")

    book_raw = S.esi.jita_orders(force=force, progress_cb=cb)
    book = {int(k): v for k, v in book_raw.items()}

    with S.lock:
        S.adjusted = adjusted
        S.cost_indices = indices
        S.book = book
        S.bpo_sell_types = S.esi.region_sell_types()

    recompute()
    _set("ready", "")


def _history_type_ids() -> list[int]:
    """Products plus every distinct material: materials need history too,
    for the achievable buy-order price (p95)."""
    ids = {p.type_id for p in S.products}
    for mats in S.materials.values():
        ids.update(m.type_id for m in mats)
    return sorted(ids)


def _hist_stats(hist: list[dict]) -> dict:
    return {
        "p5": calc.percentile_price(hist, 5),
        "p95": calc.percentile_price(hist, 95),
    }


def _utc_today() -> datetime.date:
    """ESI history dates are UTC days — the local date can be a day ahead."""
    return datetime.datetime.now(datetime.timezone.utc).date()


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _load_cached_history():
    """(volumes, hist_prices) from week-old-or-fresher on-disk cache."""
    vols, prices = {}, {}
    today = _utc_today()
    product_ids = {p.type_id for p in S.products}
    for tid in _history_type_ids():
        age = S.esi.cache_age(f"history_{tid}")
        if age is not None and age < 24 * 3600 * 7:  # tolerate week-old cache for display
            hist = S.esi.history(tid)
            prices[tid] = _hist_stats(hist)
            if tid in product_ids:
                vols[tid] = calc.avg_daily_volume(hist, today)
    return vols, prices


# ---------------- computation ----------------

def _bp_params(blueprint_type_id: int) -> tuple[int, int, int]:
    """(me, te, runs) for a blueprint: override values or global defaults."""
    st = S.settings
    ov = st.blueprint_overrides.get(str(blueprint_type_id), {})
    return (
        ov.get("me", st.blueprint_me),
        ov.get("te", st.blueprint_te),
        ov.get("runs", st.runs),
    )


def compute_row(p: sde_mod.Product) -> dict:
    st = S.settings
    mats = S.materials.get(p.blueprint_type_id, [])
    me, te, runs = _bp_params(p.blueprint_type_id)

    sci = S.cost_indices.get(st.system_id, {}).get("manufacturing", 0.0)
    broker = calc.broker_fee_rate(st.broker_relations, st.faction_standing, st.corp_standing)
    tax = calc.sales_tax_rate(st.accounting)
    t_run = calc.production_time(
        p.base_time, te, st.industry, st.advanced_industry, st.structure_time_bonus
    )
    # The cap comes from the EXACT per-run time, not the whole seconds shown.
    # Verified in game: Damage Control I at an NPC station displays 8m10s (490 s)
    # per run yet allows 5295 runs = ceil(30d / 489.6); dividing the displayed
    # 490 gives 5290, five short. The client rounds what it prints and computes
    # from full precision, exactly as it does with fee percentages.
    t_run_s = round(t_run)          # display only
    max_runs = calc.max_runs_per_job(t_run)

    # A material with no adjusted price contributes 0 to EIV, understating the
    # job cost. Deliberate: CCP publishes adjusted prices for everything that
    # actually trades, and the gap only hits 14 of ~1845 products (Project
    # Discovery rewards, structure-only components), all already unbuildable.
    eiv = calc.estimated_item_value(
        [(m.base_qty, S.adjusted.get(m.type_id, 0.0)) for m in mats], runs
    )
    jcost = calc.job_cost(eiv, sci, st.structure_tax, st.structure_cost_bonus)

    # Material cost for the whole job, two buy methods; every input is priced
    # from the Jita book by type_id (buy-order side floored by history p95)
    mc_i, mc_o, unpriceable = calc.job_material_cost(
        [(m.type_id, m.base_qty) for m in mats],
        runs, me, st.structure_material_bonus, st.structure_rig_material_bonus,
        S.book,
        {m.type_id: S.hist_prices.get(m.type_id, {}).get("p95") for m in mats},
    )

    pb = S.book.get(p.type_id, {"buy": [], "sell": []})
    units = p.quantity_per_run * runs
    sell_instant_unit = calc.volume_weighted_price(pb["buy"], units)  # dump to buy orders
    # own sell order: top ask capped by history p5 — the top ask on a thin
    # market is a wishful listing, not an achievable price
    top_ask = calc.best_price(pb["sell"])
    hist_p5 = S.hist_prices.get(p.type_id, {}).get("p5")
    sell_order_unit = calc.realistic_sell_price(top_ask, hist_p5)

    vol = S.volumes.get(p.type_id)
    job_seconds = t_run * runs

    sc = {}
    for key, (mc, buy_broker, rev, sell_broker) in {
        "buy_sell": (mc_i, False, sell_instant_unit, False),     # instant buy -> instant sell
        "buy_order": (mc_i, False, sell_order_unit, True),       # instant buy -> own sell order
        "order_sell": (mc_o, True, sell_instant_unit, False),    # own buy orders -> instant sell
        "order_order": (mc_o, True, sell_order_unit, True),      # own buy orders -> own sell order
    }.items():
        s = calc.scenario(mc, jcost, rev, units, buy_broker, sell_broker, broker, tax, job_seconds)
        sc[key] = {
            "profit": s.profit_per_job,
            "margin": s.margin_pct,
            "iph": s.isk_per_hour,
            # Same profit at the rate the market can actually absorb. Applied to
            # every scenario: order-book depth already limits a single job, while
            # daily volume is what limits *repeating* it, by any sell method.
            "iph_real": calc.market_limited_iph(s.profit_per_job, units, job_seconds, vol),
        }

    # Sanity check: an empty recipe or a production cost under 1% of the sell
    # price means degenerate SDE data (e.g. a 398 ISK battleship) — flag it.
    suspicious = not mats
    best_mc = mc_i if mc_i is not None else mc_o
    rev_unit = sell_order_unit if sell_order_unit is not None else sell_instant_unit
    if not suspicious and best_mc is not None and rev_unit:
        suspicious = (best_mc + jcost) < 0.01 * rev_unit * units

    # Liquidity flags
    low_liquidity = vol is not None and units > vol * st.max_days_to_sell
    top_bid = calc.best_price(pb["buy"])
    # no asks at all is the extreme of a thin market: fall back to history p5
    eff_ask = top_ask if top_ask is not None else hist_p5
    # No bid at all is an unbounded spread, so it counts as wide outright.
    # (Substituting 1 ISK for the missing bid used to let sub-3-ISK items pass
    # and understated the ratio whenever the real bid was below 1 ISK.)
    wide_spread = bool(
        vol is not None and vol < 100
        and eff_ask is not None
        and (not top_bid or eff_ask / top_bid > 3)
    )

    return {
        "type_id": p.type_id,
        "name": p.name,
        "group": p.group_name,
        "category": p.category_name,
        "qty_per_run": p.quantity_per_run,
        "runs": runs,
        "time_per_run_s": t_run_s,
        "max_runs_per_job": max_runs,
        # the numbers below stay arithmetically right, but such a job cannot be
        # installed in game at all — flag it rather than hide it, since it is the
        # user's own Runs setting talking, not bad data
        "over_job_limit": max_runs is not None and runs > max_runs,
        "material_cost_instant": mc_i,
        "material_cost_orders": mc_o,
        "job_cost": jcost,
        "sell_to_buy_orders": sell_instant_unit,
        "sell_via_sell_order": sell_order_unit,   # realistic: min(top ask, history p5)
        "sell_top_ask": top_ask,
        "sell_hist_p5": hist_p5,
        "daily_volume": S.volumes.get(p.type_id),
        "me": me,
        "te": te,
        "has_override": str(p.blueprint_type_id) in st.blueprint_overrides,
        # BPC-only: no market group in the SDE, or no live sell order for the
        # blueprint anywhere in The Forge (catches unseeded event BPOs)
        "bpc_only": not p.bpo_on_market or (
            S.bpo_sell_types is not None
            and p.blueprint_type_id not in S.bpo_sell_types
        ),
        "suspicious": suspicious,
        "low_liquidity": low_liquidity,
        "wide_spread": wide_spread,
        "unpriceable": bool(unpriceable),
        "scenarios": sc,
    }


def recompute():
    rows = []
    for p in S.products:
        r = compute_row(p)
        if r:
            rows.append(r)
    with S.lock:
        S.rows = rows
    log.info("Recomputed %d rows", len(rows))


# ---------------- history fetching ----------------

def _record_history(tid: int, hist: list[dict] | None, product_ids: set[int], today) -> None:
    """Store one type's history stats. `hist=None` means the fetch failed.

    An empty list is real information (the type has never traded) and is
    recorded; a failure is not, and must leave whatever we already loaded from
    cache alone — overwriting it would silently drop the p95 floor under our
    own buy orders and overstate the margin.
    """
    with S.lock:
        if hist is None:
            S.history_done += 1
            return
        S.hist_prices[tid] = _hist_stats(hist)
        if tid in product_ids:
            S.volumes[tid] = calc.avg_daily_volume(hist, today)
        S.history_done += 1


def fetch_history_background():
    ids = _history_type_ids()
    product_ids = {p.type_id for p in S.products}
    today = _utc_today()
    with S.lock:
        if S.history_running:
            return
        S.history_running = True
        S.history_total = len(ids)
        S.history_done = 0

    def one(tid):
        try:
            hist = S.esi.history(tid)
        except Exception:
            log.warning("history fetch failed for type %d, keeping cached stats", tid)
            hist = None
        _record_history(tid, hist, product_ids, today)
        with S.lock:
            done = S.history_done
        if done % 250 == 0:  # let the table fill in progressively
            recompute()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(one, ids))
    finally:
        with S.lock:
            S.history_running = False
        recompute()


# ---------------- API ----------------

def _after_bootstrap():
    if S.status == "ready":
        vols, prices = _load_cached_history()
        S.volumes.update(vols)
        S.hist_prices.update(prices)
        recompute()
        # Fresh cache entries are served from disk, so this only hits ESI
        # for types whose history is stale or missing.
        fetch_history_background()


@app.get("/")
def index():
    # The whole frontend is this one file, edited constantly during development.
    # Without Cache-Control a browser may serve it from memory without asking,
    # so a plain refresh silently shows stale JS. "no-cache" forces revalidation
    # on every load; at 43 KB over loopback the re-fetch costs nothing.
    return FileResponse(
        str(STATIC_DIR / "index.html"), headers={"Cache-Control": "no-cache"}
    )


@app.get("/api/status")
def status():
    with S.lock:
        return {
            "status": S.status,
            "progress": S.progress,
            "error": S.error,
            "products": len(S.products),
            "rows": len(S.rows),
            "history_running": S.history_running,
            "history_done": S.history_done,
            "history_total": S.history_total,
            "orders_age_s": S.esi.cache_age("orders_10000002"),
        }


@app.get("/api/settings")
def get_settings():
    d = S.settings.to_dict()
    d["current_manufacturing_index"] = S.cost_indices.get(
        S.settings.system_id, {}
    ).get("manufacturing")
    return d


@app.put("/api/settings")
def put_settings(payload: dict):
    # Merge into the current settings instead of building a fresh Settings:
    # a field absent from the payload must keep its value, not silently fall
    # back to the dataclass default. That default-reset used to wipe
    # blueprint_overrides whenever the settings modal saved a stale snapshot.
    merged = S.settings.to_dict()
    merged.update({
        k: v for k, v in payload.items() if k in config.Settings.__dataclass_fields__
    })
    s = config.Settings(**merged)
    try:
        s.validate()
    except ValueError as e:
        raise HTTPException(400, str(e))
    config.save_settings(s)
    S.settings = s
    if S.status == "ready":
        recompute()
    return s.to_dict()


# ---------------- character import (EVE SSO) ----------------
# Optional throughout: with no character connected every setting stays manual and
# the app behaves exactly as it did before this existed.

# Must match the callback registered on developers.eveonline.com character for
# character; a VPS needs its own application with its own URL.
SSO_REDIRECT = os.environ.get(
    "EVE_CALC_SSO_REDIRECT", "http://127.0.0.1:8000/api/sso/callback"
)


def _sso_skill_ids() -> dict[str, int]:
    if S.sde is None:
        raise HTTPException(503, "SDE not loaded yet")
    return {name: S.sde.type_id_by_name(name) for name in sso.SKILL_FIELDS}


def _sso_hub_owner() -> dict:
    """Corp and faction whose standings set the broker fee, from the SDE."""
    if S.sde is None:
        raise HTTPException(503, "SDE not loaded yet")
    owner = S.sde.station_owner(JITA_44)
    if owner is None:
        raise HTTPException(500, f"station {JITA_44} not found in the SDE")
    return owner


@app.get("/api/sso/status")
def sso_status():
    st = sso.load_state()
    out = st.public()
    out["redirect_uri"] = SSO_REDIRECT
    if S.sde is not None:
        out["hub_owner"] = S.sde.station_owner(JITA_44)
    return out


@app.put("/api/sso/client-id")
def sso_set_client_id(payload: dict):
    """The client id is public in the PKCE flow, so it is fine to accept and store
    it here — unlike the refresh token, which never crosses this boundary."""
    st = sso.load_state()
    st.client_id = str(payload.get("client_id", "")).strip()
    sso.save_state(st)
    return st.public()


@app.get("/api/sso/login")
def sso_login():
    st = sso.load_state()
    if not st.client_id:
        raise HTTPException(400, "set the client id first")
    verifier, challenge = sso.pkce_pair()
    state = secrets.token_urlsafe(16)
    S.sso_pending = {"state": state, "verifier": verifier}
    return {"url": sso.authorize_url(st.client_id, SSO_REDIRECT, challenge, state)}


@app.get("/api/sso/callback")
def sso_callback(code: str = "", state: str = ""):
    pending = S.sso_pending
    S.sso_pending = None          # single use, whatever happens next
    if not pending or not secrets.compare_digest(state, pending["state"]):
        # a mismatched state is the CSRF case the parameter exists for
        return HTMLResponse("<h3>Login state mismatch — start again.</h3>", 400)
    try:
        tok = sso.exchange_code(
            sso.load_state().client_id, code, pending["verifier"], USER_AGENT
        )
        char_id, char_name = sso.character_from_access_token(tok["access_token"])
    except (sso.SSOError, KeyError) as e:
        return HTMLResponse(f"<h3>Login failed</h3><pre>{e}</pre>", 400)

    st = sso.load_state()
    st.refresh_token = tok["refresh_token"]
    st.character_id, st.character_name = char_id, char_name
    sso.save_state(st)
    log.info("SSO connected as %s (%d)", char_name, char_id)
    return HTMLResponse(
        f"<h3>Connected as {char_name}</h3>"
        "<p>Close this tab and press Import in the settings.</p>"
    )


@app.post("/api/sso/import")
def sso_import():
    """Pull skills and standings onto the existing settings.

    Writes through the same merge path a manual edit uses, so nothing downstream
    can tell the difference and none of the other settings are disturbed.
    """
    st = sso.load_state()
    if not st.connected:
        raise HTTPException(400, "no character connected")
    owner = _sso_hub_owner()
    skill_ids = _sso_skill_ids()

    tok = sso.refresh_tokens(st.client_id, st.refresh_token, USER_AGENT)
    # store first: the returned refresh token may differ from the one we sent,
    # and losing the new one would strand the connection
    st.refresh_token = tok.get("refresh_token", st.refresh_token)
    sso.save_state(st)
    access = tok["access_token"]

    skills = S.esi.character_skills(st.character_id, access)
    standings = S.esi.character_standings(st.character_id, access)
    imported = sso.skills_to_settings(skills, skill_ids)
    imported.update(
        sso.standings_for(standings, owner["corporation_id"], owner["faction_id"])
    )

    before = {k: getattr(S.settings, k) for k in imported}
    put_settings(imported)          # validates, persists and recomputes
    st.imported_at = _utc_now_iso()
    sso.save_state(st)
    return {"imported": imported, "before": before, "status": st.public()}


@app.post("/api/sso/logout")
def sso_logout():
    """Forget the character. Settings keep the values that were imported — they
    are ordinary settings now, and wiping them would be a surprise."""
    st = sso.load_state()
    st.refresh_token = ""
    st.character_id, st.character_name, st.imported_at = 0, "", ""
    sso.save_state(st)
    return st.public()


@app.get("/api/fees")
def fees(
    broker_relations: float = 0.0,
    faction_standing: float = 0.0,
    corp_standing: float = 0.0,
    accounting: float = 0.0,
):
    """Live fee preview for the values being typed in the settings modal.

    The formulas stay in calc so the page never carries a second copy that can
    drift — the NPC broker floor already moved from 0.5% to 1% once. Skill levels
    are floats here on purpose: this previews half-typed input and must not 422.

    Standings affect the broker fee and nothing else the app models. (In game
    they also cut the NPC reprocessing tax, which is out of scope, and they do
    not touch industry job cost at all.)
    """
    uncapped = calc.broker_fee_rate_uncapped(broker_relations, faction_standing, corp_standing)
    fee = calc.broker_fee_rate(broker_relations, faction_standing, corp_standing)
    fee_no_standing = calc.broker_fee_rate(broker_relations)
    tax = calc.sales_tax_rate(accounting)
    return {
        "broker_fee_pct": fee * 100,
        "broker_fee_uncapped_pct": uncapped * 100,
        "at_floor": uncapped <= calc.BROKER_FEE_FLOOR,
        "floor_pct": calc.BROKER_FEE_FLOOR * 100,
        "base_pct": calc.BROKER_FEE_BASE * 100,
        "from_skill_pct": calc.BROKER_FEE_PER_SKILL * broker_relations * 100,
        "from_faction_pct": calc.BROKER_FEE_PER_FACTION * faction_standing * 100,
        "from_corp_pct": calc.BROKER_FEE_PER_CORP * corp_standing * 100,
        # what the standings actually buy, after the floor is taken into account
        "standings_saving_pct": (fee_no_standing - fee) * 100,
        "sales_tax_pct": tax * 100,
        "total_sell_pct": (fee + tax) * 100,
    }


@app.get("/api/items")
def items():
    # Keep serving the last computed rows while a price refresh is in flight:
    # that takes minutes, and blanking the table for all of it is worse than
    # showing numbers a few minutes old — the header states the order age.
    with S.lock:
        if not S.rows:
            raise HTTPException(503, f"not ready: {S.status}")
        return {"rows": S.rows, "stale": S.status != "ready"}


_RIG_CATEGORY_ORDER = {
    sde_mod.rig_category(size): i
    for i, size in enumerate(sorted(sde_mod.RIG_SIZE_NAMES))
}


def _category_sort_key(name: str) -> tuple[str, int]:
    """Alphabetical, except the rig categories sort together under "Rigs" and by
    size rather than alphabetically (which would read Capital, Large, Medium,
    Small — adjacent but backwards)."""
    if name in _RIG_CATEGORY_ORDER:
        return ("Rigs", _RIG_CATEGORY_ORDER[name])
    return (name, 0)


@app.get("/api/categories")
def categories():
    # Derived from the loaded products so the list always matches the table
    # (a raw SDE query once returned only "Blueprint" — the blueprint's own
    # category — making the filter useless).
    if not S.products:
        raise HTTPException(503, "not ready")
    return sorted({p.category_name for p in S.products}, key=_category_sort_key)


@app.get("/api/systems")
def systems(q: str = ""):
    if S.sde is None:
        raise HTTPException(503, "not ready")
    out = []
    for s in S.sde.search_systems(q):
        idx = S.cost_indices.get(s["system_id"], {}).get("manufacturing")
        out.append({**s, "manufacturing_index": idx})
    return out


@app.get("/api/item/{type_id}")
def item_detail(type_id: int):
    if not S.rows:  # as with /api/items, stale detail beats a dead modal
        raise HTTPException(503, f"not ready: {S.status}")
    p = next((x for x in S.products if x.type_id == type_id), None)
    if p is None:
        raise HTTPException(404, "unknown product")
    st = S.settings
    me, _te, bp_runs = _bp_params(p.blueprint_type_id)
    mats = []
    for m in S.materials.get(p.blueprint_type_id, []):
        qty = calc.material_quantity(
            m.base_qty, bp_runs, me, st.structure_material_bonus, st.structure_rig_material_bonus
        )
        b = S.book.get(m.type_id, {"buy": [], "sell": []})
        mats.append({
            "type_id": m.type_id,
            "name": m.name,
            "base_qty": m.base_qty,
            "qty_with_me": qty,
            "vw_sell_price": calc.volume_weighted_price(b["sell"], qty),
            "best_buy_price": calc.best_price(b["buy"]),
            "buy_realistic": calc.realistic_buy_price(
                calc.best_price(b["buy"]), S.hist_prices.get(m.type_id, {}).get("p95")
            ),
            "adjusted_price": S.adjusted.get(m.type_id, 0.0),
        })
    row = compute_row(p)
    return {"row": row, "materials": mats, "blueprint_type_id": p.blueprint_type_id}


@app.put("/api/blueprint/{blueprint_type_id}")
def set_blueprint_override(blueprint_type_id: int, payload: dict):
    """Set a per-blueprint ME/TE/runs override; all-null payload clears it."""
    st = S.settings
    key = str(blueprint_type_id)
    prev = st.blueprint_overrides.get(key)
    entry = {k: payload[k] for k in ("me", "te", "runs") if payload.get(k) is not None}
    if entry:
        st.blueprint_overrides[key] = entry
    else:
        st.blueprint_overrides.pop(key, None)
    try:
        config.save_settings(st)
    except ValueError as e:
        # restore what was there — a rejected edit must not delete a working
        # override, which dropping the key outright used to do
        if prev is None:
            st.blueprint_overrides.pop(key, None)
        else:
            st.blueprint_overrides[key] = prev
        raise HTTPException(400, str(e))
    if S.status == "ready":
        recompute()
    return {"override": st.blueprint_overrides.get(key)}


@app.post("/api/refresh")
def refresh():
    if S.status not in ("ready", "error"):
        raise HTTPException(409, "busy")
    threading.Thread(target=lambda: refresh_prices(force=True), daemon=True).start()
    return {"ok": True}


@app.post("/api/refresh-history")
def refresh_history():
    if S.status != "ready":
        raise HTTPException(503, "not ready")
    threading.Thread(target=fetch_history_background, daemon=True).start()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
