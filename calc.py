"""Pure calculation core: EVE industry math, fees, order-book pricing.

Everything here is side-effect free and covered by tests. T2/invention and
reactions later only need new activity inputs — the fee/pricing helpers are
activity-agnostic.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

SCC_SURCHARGE = 0.04  # 4% of EIV
MAX_JOB_SECONDS = 30 * 24 * 3600  # a job cannot be installed past 30 days


# ---------- materials ----------

def material_quantity(
    base_qty: int,
    runs: int,
    me: int,
    structure_bonus_pct: float = 0.0,
    rig_bonus_pct: float = 0.0,
) -> int:
    """Materials needed for `runs` runs, EVE rounding rules.

    Bonuses are multiplicative; the result of the multiplier chain is rounded
    to 2 decimals, then ceiled, and can never drop below 1 per run.
    """
    mult = (1 - me / 100) * (1 - structure_bonus_pct / 100) * (1 - rig_bonus_pct / 100)
    qty = base_qty * runs * mult
    return max(runs, math.ceil(round(qty, 2)))


# ---------- job cost ----------

def job_cost(
    eiv: float,
    system_cost_index: float,
    structure_tax_pct: float,
    structure_cost_bonus_pct: float = 0.0,
) -> float:
    """Install cost.

    EIV * SCI * (1 - structure job cost bonus)   gross job cost
    + EIV * facility tax                          owner-set tax, on full EIV
    + EIV * 4%                                    SCC surcharge, on full EIV

    structure_cost_bonus_pct: e.g. 3 for a Raitaru, 4 Azbel, 5 Sotiyo.
    """
    return eiv * (
        system_cost_index * (1 - structure_cost_bonus_pct / 100)
        + structure_tax_pct / 100
        + SCC_SURCHARGE
    )


def estimated_item_value(base_materials: list[tuple[int, float]], runs: int = 1) -> float:
    """EIV = sum(base_qty * adjusted_price) * runs. Materials as (base_qty, adjusted_price)."""
    return sum(q * p for q, p in base_materials) * runs


# ---------- fees ----------

BROKER_FEE_BASE = 0.03
BROKER_FEE_FLOOR = 0.01           # NPC stations never go below 1%
BROKER_FEE_PER_SKILL = 0.003      # per level of Broker Relations
BROKER_FEE_PER_FACTION = 0.0003   # per point of standing toward the owning faction
BROKER_FEE_PER_CORP = 0.0002      # per point of standing toward the owning corp


def broker_fee_rate_uncapped(
    broker_relations: int,
    faction_standing: float = 0.0,
    corp_standing: float = 0.0,
) -> float:
    """The fee before the floor is applied.

    Only useful for telling the user when more standing has stopped helping —
    never charge this, charge broker_fee_rate.
    """
    return (
        BROKER_FEE_BASE
        - BROKER_FEE_PER_SKILL * broker_relations
        - BROKER_FEE_PER_FACTION * faction_standing
        - BROKER_FEE_PER_CORP * corp_standing
    )


def broker_fee_rate(
    broker_relations: int,
    faction_standing: float = 0.0,
    corp_standing: float = 0.0,
) -> float:
    """NPC station broker fee as a fraction. Floor 1%.

    Standings are the **unmodified** (base) values: the fee ignores standing
    skills entirely, so Connections/Diplomacy/Social must not appear here even
    though they raise effective standing. Broker Relations is the only skill
    that reduces this fee.
    """
    return max(
        BROKER_FEE_FLOOR,
        broker_fee_rate_uncapped(broker_relations, faction_standing, corp_standing),
    )


def sales_tax_rate(accounting: int) -> float:
    """Sales tax as a fraction: 7.5% reduced 11% per Accounting level."""
    return 0.075 * (1 - 0.11 * accounting)


# ---------- production time ----------

def production_time(
    base_time: float,
    te: int,
    industry: int,
    advanced_industry: int,
    structure_time_bonus_pct: float = 0.0,
) -> float:
    """Seconds per run with TE, skills (4%/3% per level) and structure bonus."""
    return (
        base_time
        * (1 - te / 100)
        * (1 - 0.04 * industry)
        * (1 - 0.03 * advanced_industry)
        * (1 - structure_time_bonus_pct / 100)
    )


def max_runs_per_job(time_per_run_s: float) -> int | None:
    """Largest run count installable in one job: ceil(30 days / time per run).

    EVE refuses a manufacturing job whose total time would run past 30 days, so
    the cap depends on TE, skills and structure bonuses — pass the already
    modified per-run time, **unrounded**: the client rounds the duration it
    displays but computes the cap from full precision, so feeding it whole
    seconds undercounts (verified in game, see tests).

    The accepted maximum lands one run *over* the limit — Damage Control I caps
    at 5295 runs of 489.6 s = 30.005 days, where 5294 would still fit inside —
    hence ceil rather than floor.

    Items whose single run already exceeds 30 days (capitals, supers) are exempt
    from the cap, so the floor of 1 is a real allowance, not a clamp.
    None when the time is unknown.
    """
    if time_per_run_s <= 0:
        return None
    return max(1, math.ceil(MAX_JOB_SECONDS / time_per_run_s))


# ---------- order book pricing ----------

def volume_weighted_price(orders: list[list[float]], quantity: float) -> float | None:
    """Average unit price to fill `quantity` walking the book in given order.

    `orders` is [[price, volume], ...] already sorted best-first
    (ascending for sells, descending for buys). None if depth is insufficient.
    """
    if quantity <= 0:
        return None
    remaining = quantity
    cost = 0.0
    for price, volume in orders:
        take = min(remaining, volume)
        cost += take * price
        remaining -= take
        if remaining <= 0:
            return cost / quantity
    return None


def best_price(orders: list[list[float]]) -> float | None:
    return orders[0][0] if orders else None


# ---------- realistic pricing from market history ----------

def percentile_price(history: list[dict], pct: float, days: int = 30) -> float | None:
    """Percentile (linear interpolation) of daily average prices.

    Takes the last `days` history entries (ESI only lists days with trades,
    so this is "the last up-to-30 traded days"). None if there is no history.
    """
    prices = sorted(d["average"] for d in history[-days:] if d.get("average") is not None)
    if not prices:
        return None
    k = (len(prices) - 1) * pct / 100
    lo, hi = math.floor(k), math.ceil(k)
    return prices[lo] + (prices[hi] - prices[lo]) * (k - lo)


def avg_daily_volume(history: list[dict], today: date, days: int = 7) -> float:
    """Units traded per CALENDAR day over the `days` days ending yesterday.

    ESI history lists only days that had trades, so averaging the last N
    entries by their own count gives volume per *traded* day: an item that
    traded seven times in a year then looks as liquid as one trading daily.
    Summing over a fixed calendar window instead makes untraded days count as
    the zeroes they are, and an item that stopped trading decays to 0.

    The window ends at `today - 1`: ESI publishes a day's aggregate only after
    that day closes in UTC, so today never has usable data. `today` is passed
    in rather than read from the clock to keep this module deterministic.
    """
    if not history or days <= 0:
        return 0.0
    last = today - timedelta(days=1)
    first = last - timedelta(days=days - 1)
    total = 0.0
    for entry in reversed(history):  # ESI returns ascending by date
        try:
            day = date.fromisoformat(entry["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if day > last:
            continue
        if day < first:
            break
        total += entry.get("volume") or 0
    return total / days


def realistic_sell_price(top_ask: float | None, hist_low: float | None) -> float | None:
    """Achievable own-sell-order price: the top ask on thin markets is a
    wishful listing, so cap it by a low percentile of traded prices."""
    if top_ask is None:
        return hist_low
    if hist_low is None:
        return top_ask
    return min(top_ask, hist_low)


def realistic_buy_price(top_bid: float | None, hist_high: float | None) -> float | None:
    """Achievable own-buy-order price: a lowball bid on an illiquid material
    won't fill, so floor it by a high percentile of traded prices."""
    if top_bid is None:
        return hist_high
    if hist_high is None:
        return top_bid
    return max(top_bid, hist_high)


def job_material_cost(
    materials: list[tuple[int, int]],
    runs: int,
    me: int,
    structure_bonus_pct: float,
    rig_bonus_pct: float,
    book: dict,
    hist_p95: dict | None = None,
) -> tuple[float | None, float | None, list[int]]:
    """Material cost for a whole job, both buy methods.

    Every input — mineral, component or finished module — is priced from the
    order book by its type_id; nothing ever falls back to 0 or adjusted price.

    materials: [(type_id, base_qty), ...]
    book:      {type_id: {"buy": [[price, vol], ...], "sell": [...]}}
    hist_p95:  {type_id: p95 or None} floor for own buy orders

    Returns (cost_instant, cost_orders, unpriceable): a cost is None when any
    input can't be priced that way; unpriceable lists inputs whose required
    quantity can't be bought from sell orders at all.
    """
    hist_p95 = hist_p95 or {}
    cost_instant = cost_orders = 0.0
    instant_ok = orders_ok = True
    unpriceable = []
    for type_id, base_qty in materials:
        qty = material_quantity(base_qty, runs, me, structure_bonus_pct, rig_bonus_pct)
        b = book.get(type_id, {"buy": [], "sell": []})
        vw = volume_weighted_price(b["sell"], qty)
        bid = realistic_buy_price(best_price(b["buy"]), hist_p95.get(type_id))
        if vw is None:
            instant_ok = False
            unpriceable.append(type_id)
        else:
            cost_instant += vw * qty
        if bid is None:
            orders_ok = False
        else:
            cost_orders += bid * qty
    return (
        cost_instant if instant_ok else None,
        cost_orders if orders_ok else None,
        unpriceable,
    )


# ---------- achievable rate ----------

def market_limited_iph(
    profit: float | None,
    units: int,
    job_seconds: float,
    daily_volume: float | None,
) -> float | None:
    """Profit per hour at the rate actually achievable, not just producible.

    Two ceilings bind the operation. The line turns out `units / job_hours` items
    an hour; the market only absorbs `daily_volume / 24`. The achievable rate is
    the lower of the two — production and selling pipeline (the next job runs
    while the last batch sells), so the binding constraint wins instead of the
    two waits adding up.

    Deliberately a rate times profit-per-unit rather than min() of two ISK/h
    figures: on a loss-making item min() would call the slower operation *worse*,
    when producing slower in fact loses less per hour.

    None when there is nothing to go on — no profit, no output, or the volume has
    not been fetched yet. A volume of 0 is real information (nothing traded in the
    window, so nothing can be sold) and gives 0.
    """
    if profit is None or daily_volume is None or units <= 0 or job_seconds <= 0:
        return None
    rate_line = units / (job_seconds / 3600)
    rate_market = daily_volume / 24
    return profit / units * min(rate_line, rate_market)


# ---------- scenarios ----------

@dataclass
class Scenario:
    """Everything here is for a whole job, not for one run: material rounding is
    per job, so a job's numbers are not a run's times `runs`."""

    profit_per_job: float | None
    margin_pct: float | None
    isk_per_hour: float | None


def scenario(
    material_cost: float | None,
    job_cost_isk: float,
    unit_revenue: float | None,
    units: int,
    buy_broker: bool,
    sell_broker: bool,
    broker_rate: float,
    tax_rate: float,
    job_seconds: float,
) -> Scenario:
    """One cell of the 2x2 matrix, for one whole job.

    material_cost: ISK for the job's materials at the chosen buy method
                   (already the raw order price; broker fee added here if
                   buying via own buy orders).
    unit_revenue:  raw unit price at the chosen sell method; sales tax and
                   (optionally) broker fee are deducted here.
    units:         items the job produces in total (qty per run x runs).
    job_seconds:   the job's whole duration, not the time of a single run.
    """
    if material_cost is None or unit_revenue is None:
        return Scenario(None, None, None)

    cost = material_cost * (1 + broker_rate if buy_broker else 1) + job_cost_isk
    revenue = unit_revenue * units
    revenue -= revenue * tax_rate
    if sell_broker:
        revenue -= unit_revenue * units * broker_rate

    profit = revenue - cost
    margin = (profit / cost * 100) if cost > 0 else None
    iph = profit / (job_seconds / 3600) if job_seconds > 0 else None
    return Scenario(profit, margin, iph)
