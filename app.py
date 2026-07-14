"""FastAPI app: bootstrap (SDE download), price refresh, table & detail APIs.

Run: uvicorn app:app  (or `python app.py`)
"""
from __future__ import annotations

import concurrent.futures
import logging
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import calc
import config
import sde as sde_mod
from esi import ESIClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

app = FastAPI(title="EVE T1 Manufacturing Profit Calculator")

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
        self.rows: list[dict] = []

        self.history_total = 0
        self.history_done = 0
        self.history_running = False


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

    recompute()
    _set("ready", "")


def _load_cached_volumes():
    vols = {}
    for p in S.products:
        age = S.esi.cache_age(f"history_{p.type_id}")
        if age is not None and age < 24 * 3600 * 7:  # tolerate week-old cache for display
            hist = S.esi.history(p.type_id)
            vols[p.type_id] = _avg_daily_volume(hist)
    return vols


def _avg_daily_volume(history: list[dict], days: int = 7) -> float:
    if not history:
        return 0.0
    recent = history[-days:]
    return sum(d["volume"] for d in recent) / max(1, len(recent))


# ---------------- computation ----------------

def compute_row(p: sde_mod.Product) -> dict:
    st = S.settings
    mats = S.materials.get(p.blueprint_type_id, [])
    if not mats:
        return {}

    sci = S.cost_indices.get(st.system_id, {}).get("manufacturing", 0.0)
    broker = calc.broker_fee_rate(st.broker_relations, st.faction_standing, st.corp_standing)
    tax = calc.sales_tax_rate(st.accounting)
    t_run = calc.production_time(
        p.base_time, st.blueprint_te, st.industry, st.advanced_industry, st.structure_time_bonus
    )

    eiv = calc.estimated_item_value(
        [(m.base_qty, S.adjusted.get(m.type_id, 0.0)) for m in mats]
    )
    jcost = calc.job_cost(eiv, sci, st.structure_tax)

    # Material cost per run, two buy methods
    cost_instant = 0.0   # buy from sell orders, volume-weighted
    cost_orders = 0.0    # own buy orders at best bid (broker fee added in scenario)
    instant_ok = orders_ok = True
    for m in mats:
        qty = calc.material_quantity(
            m.base_qty, 1, st.blueprint_me, st.structure_material_bonus, st.structure_rig_material_bonus
        )
        b = S.book.get(m.type_id, {"buy": [], "sell": []})
        vw = calc.volume_weighted_price(b["sell"], qty)
        bid = calc.best_price(b["buy"])
        if vw is None:
            instant_ok = False
        else:
            cost_instant += vw * qty
        if bid is None:
            orders_ok = False
        else:
            cost_orders += bid * qty

    pb = S.book.get(p.type_id, {"buy": [], "sell": []})
    sell_instant_unit = calc.volume_weighted_price(pb["buy"], p.quantity_per_run)  # dump to buy orders
    sell_order_unit = calc.best_price(pb["sell"])                                  # own sell order at best ask

    mc_i = cost_instant if instant_ok else None
    mc_o = cost_orders if orders_ok else None

    sc = {}
    for key, (mc, buy_broker, rev, sell_broker) in {
        "buy_sell": (mc_i, False, sell_instant_unit, False),     # instant buy -> instant sell
        "buy_order": (mc_i, False, sell_order_unit, True),       # instant buy -> own sell order
        "order_sell": (mc_o, True, sell_instant_unit, False),    # own buy orders -> instant sell
        "order_order": (mc_o, True, sell_order_unit, True),      # own buy orders -> own sell order
    }.items():
        s = calc.scenario(mc, jcost, rev, p.quantity_per_run, buy_broker, sell_broker, broker, tax, t_run)
        sc[key] = {"profit": s.profit_per_run, "margin": s.margin_pct, "iph": s.isk_per_hour}

    return {
        "type_id": p.type_id,
        "name": p.name,
        "group": p.group_name,
        "category": p.category_name,
        "qty_per_run": p.quantity_per_run,
        "time_per_run_s": round(t_run),
        "material_cost_instant": mc_i,
        "material_cost_orders": mc_o,
        "job_cost": jcost,
        "sell_to_buy_orders": sell_instant_unit,
        "sell_via_sell_order": sell_order_unit,
        "daily_volume": S.volumes.get(p.type_id),
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

def fetch_history_background():
    with S.lock:
        if S.history_running:
            return
        S.history_running = True
        S.history_total = len(S.products)
        S.history_done = 0

    def one(p):
        try:
            hist = S.esi.history(p.type_id)
        except Exception:
            log.warning("history fetch failed for type %d", p.type_id, exc_info=True)
            hist = []
        vol = _avg_daily_volume(hist)
        with S.lock:
            S.volumes[p.type_id] = vol
            S.history_done += 1
            done = S.history_done
        if done % 250 == 0:  # let the table fill in progressively
            recompute()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(one, S.products))
    finally:
        with S.lock:
            S.history_running = False
        recompute()


# ---------------- API ----------------

@app.on_event("startup")
def on_startup():
    threading.Thread(target=lambda: (bootstrap(), _after_bootstrap()), daemon=True).start()


def _after_bootstrap():
    if S.status == "ready":
        S.volumes.update(_load_cached_volumes())
        recompute()
        # Fresh cache entries are served from disk, so this only hits ESI
        # for types whose history is stale or missing.
        fetch_history_background()


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


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
    return S.settings.to_dict()


@app.put("/api/settings")
def put_settings(payload: dict):
    s = config.Settings(**{
        k: v for k, v in payload.items() if k in config.Settings.__dataclass_fields__
    })
    try:
        s.validate()
    except ValueError as e:
        raise HTTPException(400, str(e))
    config.save_settings(s)
    S.settings = s
    if S.status == "ready":
        recompute()
    return s.to_dict()


@app.get("/api/items")
def items():
    if S.status != "ready":
        raise HTTPException(503, f"not ready: {S.status}")
    with S.lock:
        return {"rows": S.rows}


@app.get("/api/categories")
def categories():
    if S.sde is None:
        raise HTTPException(503, "not ready")
    return S.sde.categories()


@app.get("/api/systems")
def systems(q: str = ""):
    if S.sde is None:
        raise HTTPException(503, "not ready")
    return S.sde.search_systems(q)


@app.get("/api/item/{type_id}")
def item_detail(type_id: int):
    if S.status != "ready":
        raise HTTPException(503, "not ready")
    p = next((x for x in S.products if x.type_id == type_id), None)
    if p is None:
        raise HTTPException(404, "unknown product")
    st = S.settings
    mats = []
    for m in S.materials.get(p.blueprint_type_id, []):
        qty = calc.material_quantity(
            m.base_qty, 1, st.blueprint_me, st.structure_material_bonus, st.structure_rig_material_bonus
        )
        b = S.book.get(m.type_id, {"buy": [], "sell": []})
        mats.append({
            "type_id": m.type_id,
            "name": m.name,
            "base_qty": m.base_qty,
            "qty_with_me": qty,
            "vw_sell_price": calc.volume_weighted_price(b["sell"], qty),
            "best_buy_price": calc.best_price(b["buy"]),
            "adjusted_price": S.adjusted.get(m.type_id, 0.0),
        })
    row = compute_row(p)
    return {"row": row, "materials": mats, "blueprint_type_id": p.blueprint_type_id}


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
