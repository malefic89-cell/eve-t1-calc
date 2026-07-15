"""Pure calculation core: EVE industry math, fees, order-book pricing.

Everything here is side-effect free and covered by tests. T2/invention and
reactions later only need new activity inputs — the fee/pricing helpers are
activity-agnostic.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SCC_SURCHARGE = 0.04  # 4% of EIV


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

def broker_fee_rate(
    broker_relations: int,
    faction_standing: float = 0.0,
    corp_standing: float = 0.0,
) -> float:
    """NPC station broker fee as a fraction. Floor 1%."""
    rate = 0.03 - 0.003 * broker_relations - 0.0003 * faction_standing - 0.0002 * corp_standing
    return max(0.01, rate)


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


# ---------- scenarios ----------

@dataclass
class Scenario:
    profit_per_run: float | None
    margin_pct: float | None
    isk_per_hour: float | None


def scenario(
    material_cost: float | None,
    job_cost_isk: float,
    unit_revenue: float | None,
    units_per_run: int,
    buy_broker: bool,
    sell_broker: bool,
    broker_rate: float,
    tax_rate: float,
    time_per_run_s: float,
) -> Scenario:
    """One cell of the 2x2 matrix.

    material_cost: ISK for one run's materials at the chosen buy method
                   (already the raw order price; broker fee added here if
                   buying via own buy orders).
    unit_revenue:  raw unit price at the chosen sell method; sales tax and
                   (optionally) broker fee are deducted here.
    """
    if material_cost is None or unit_revenue is None:
        return Scenario(None, None, None)

    cost = material_cost * (1 + broker_rate if buy_broker else 1) + job_cost_isk
    revenue = unit_revenue * units_per_run
    revenue -= revenue * tax_rate
    if sell_broker:
        revenue -= unit_revenue * units_per_run * broker_rate

    profit = revenue - cost
    margin = (profit / cost * 100) if cost > 0 else None
    iph = profit / (time_per_run_s / 3600) if time_per_run_s > 0 else None
    return Scenario(profit, margin, iph)
