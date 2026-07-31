import math
from datetime import date, timedelta

import pytest

import calc


class TestMaterialQuantity:
    def test_no_bonuses(self):
        assert calc.material_quantity(100, 1, 0) == 100

    def test_me10_only(self):
        assert calc.material_quantity(100, 1, 10) == 90

    def test_rounding_up(self):
        # 3 * 0.9 = 2.7 -> ceil -> 3
        assert calc.material_quantity(3, 1, 10) == 3

    def test_minimum_one_per_run(self):
        # base 1 with any ME can never go below 1
        assert calc.material_quantity(1, 1, 10) == 1
        assert calc.material_quantity(1, 5, 10) == 5  # never below runs

    def test_multiplicative_bonuses(self):
        # 100 * 0.9 * 0.99 * 0.98 = 87.318 -> round(.., 2)=87.32 -> ceil -> 88
        assert calc.material_quantity(100, 1, 10, 1.0, 2.0) == 88

    def test_multiple_runs_rounded_once(self):
        # 10 runs of base 3, ME10: 30 * 0.9 = 27 exactly (not 3->ceil per run = 30)
        assert calc.material_quantity(3, 10, 10) == 27

    def test_round_before_ceil(self):
        # EVE rounds to 2 decimals before ceiling: 99.999 -> round -> 100.00 -> 100,
        # not ceil(99.999) = 100 vs a fractional artifact like 99.0000001 -> 100.
        # 0.001% structure bonus: 100 * (1 - 0.00001) = 99.999 -> round -> 100.0 -> 100
        assert calc.material_quantity(100, 1, 0, 0.001) == 100
        # while 1% gives a real reduction: 99.0 -> 99
        assert calc.material_quantity(100, 1, 0, 1.0) == 99


class TestJobCost:
    def test_basic(self):
        # EIV 1M, SCI 5%, tax 1%: 1M*0.05 + 1M*0.01 + 1M*0.04 = 50000 + 10000 + 40000
        assert calc.job_cost(1_000_000, 0.05, 1.0) == pytest.approx(100_000)

    def test_zero_index(self):
        assert calc.job_cost(1_000_000, 0.0, 0.0) == pytest.approx(40_000)

    def test_structure_bonus_applies_to_sci_part_only(self):
        # Raitaru 3%: 1M*0.05*0.97 + 1M*0.01 + 1M*0.04 = 48500 + 10000 + 40000
        assert calc.job_cost(1_000_000, 0.05, 1.0, 3.0) == pytest.approx(98_500)

    def test_salvager_in_game_reference(self):
        # Verified in-game 2026-07-15: Salvager I at a 3%-tax Raitaru in
        # Perimeter (SCI 5.15%), EIV 31206.59 -> 3743 ISK
        assert calc.job_cost(31206.588, 0.0515, 3.0, 3.0) == pytest.approx(3743, abs=1)

    def test_eiv(self):
        assert calc.estimated_item_value([(10, 5.0), (2, 100.0)], runs=2) == pytest.approx(500.0)


class TestFees:
    def test_broker_base(self):
        assert calc.broker_fee_rate(0) == pytest.approx(0.03)

    def test_broker_max_skill(self):
        assert calc.broker_fee_rate(5) == pytest.approx(0.015)

    def test_broker_with_standings(self):
        # BR5, faction 10, corp 10: 3% - 1.5% - 0.3% - 0.2% = 1.0%
        assert calc.broker_fee_rate(5, 10.0, 10.0) == pytest.approx(0.01)

    def test_broker_floor(self):
        # NPC station floor is 1% (EVE Uni wiki, checked 2026-07)
        assert calc.broker_fee_rate(5, 50.0, 50.0) == pytest.approx(0.01)

    def test_uncapped_shows_where_standing_stops_helping(self):
        # the uncapped rate keeps falling past the floor, which is how the UI
        # knows to say "more standing changes nothing"
        # 3% - 1.5% skill - 1.5% faction - 1.0% corp = -1%, well past the floor
        assert calc.broker_fee_rate_uncapped(5, 50.0, 50.0) == pytest.approx(-0.01)
        assert calc.broker_fee_rate_uncapped(5, 10.0, 10.0) == pytest.approx(0.01)
        assert calc.broker_fee_rate_uncapped(0) == pytest.approx(calc.BROKER_FEE_BASE)

    def test_negative_standing_raises_the_fee(self):
        # -10/-10 adds 0.3% + 0.2% on top of the 3% base; no ceiling involved
        assert calc.broker_fee_rate(0, -10.0, -10.0) == pytest.approx(0.035)

    # ---- in-game reference, Logic Circuit, verified 2026-07-30 ----
    # A sell order priced at 2,316,000 ISK was charged 24,881.59 ISK, i.e.
    # 1.0743346%. The client displayed "1.07%" and standings "7.89 / 9.44" —
    # every percentage in that window is rounded to hundredths for display while
    # the full-precision value is what gets charged, so the ISK figure is the
    # only trustworthy calibration target.
    GAME_PRICE = 2_316_000.0
    GAME_FEE_ISK = 24_881.59

    def test_logic_circuit_in_game_reference(self):
        fee = self.GAME_PRICE * calc.broker_fee_rate(5, 7.89, 9.44)
        # Standings entered to 2 decimals can be off by 0.005 each, which moves
        # the rate by at most 0.03%*0.005 + 0.02%*0.005 = 0.00025 points —
        # 2.5 ISK per million of order value. The residual here is 3.83 ISK.
        assert fee == pytest.approx(self.GAME_FEE_ISK, abs=self.GAME_PRICE * 2.5e-6)

    def test_standing_display_rounding_explains_the_residual(self):
        # Standings carry more decimals than the client shows: 7.8925 / 9.4445
        # both display as 7.89 / 9.44 and land exactly on the charged amount, so
        # the gap above is display precision, not an error in the formula.
        fee = self.GAME_PRICE * calc.broker_fee_rate(5, 7.8925, 9.4445)
        assert fee == pytest.approx(self.GAME_FEE_ISK, abs=0.01)
        assert round(7.8925, 2) == 7.89 and round(9.4445, 2) == 9.44

    def test_full_precision_standings_reproduce_the_charge_exactly(self):
        """The residual in the test above is standing precision — now measured,
        not inferred. An ESI import returned 7.892620134 / 9.444335456, which the
        client displays as 7.89 / 9.44, and those land on the charged amount to a
        thousandth of an ISK where the rounded pair misses by 3.83.

        It also settles that ESI reports **base** standings: an effective value,
        inflated by Connections, would be higher and could not reconcile.
        """
        rate = calc.broker_fee_rate(5, 7.892620134, 9.444335456)
        assert self.GAME_PRICE * rate == pytest.approx(self.GAME_FEE_ISK, abs=0.01)
        assert round(7.892620134, 2) == 7.89 and round(9.444335456, 2) == 9.44

    def test_displayed_percent_is_not_the_charged_rate(self):
        # taking the client's rounded "1.07%" at face value misses by ~100 ISK
        assert self.GAME_PRICE * 0.0107 == pytest.approx(24_781.20, abs=0.01)
        assert abs(self.GAME_PRICE * 0.0107 - self.GAME_FEE_ISK) > 100

    def test_sales_tax_zero_skill(self):
        assert calc.sales_tax_rate(0) == pytest.approx(0.075)

    def test_sales_tax_max_skill(self):
        assert calc.sales_tax_rate(5) == pytest.approx(0.075 * 0.45)


class TestProductionTime:
    def test_all_bonuses(self):
        t = calc.production_time(1000, te=20, industry=5, advanced_industry=5,
                                 structure_time_bonus_pct=15)
        assert t == pytest.approx(1000 * 0.8 * 0.8 * 0.85 * 0.85)

    def test_no_bonuses(self):
        assert calc.production_time(1000, 0, 0, 0) == 1000


class TestMaxRunsPerJob:
    def test_damage_control_in_game_reference(self):
        """Verified in game 2026-07-30. Damage Control I, NPC station, TE 20,
        Industry 5, Advanced Industry 5: the client showed 8m10s per run and
        capped Runs at 5295. This pins both open questions at once."""
        t = calc.production_time(900, te=20, industry=5, advanced_industry=5)
        assert round(t) == 490                      # matches the displayed 8m10s
        assert calc.max_runs_per_job(t) == 5295     # the observed cap

        # 1. The cap uses the EXACT per-run time. The displayed whole seconds
        #    would give 5290 — five short — so don't "simplify" to round(t).
        assert calc.max_runs_per_job(round(t)) == 5290

        # 2. It is ceil, not floor: 5295 runs overshoot 30 days while 5294 fit,
        #    so the game grants the run that crosses the line.
        assert 5295 * t > calc.MAX_JOB_SECONDS
        assert 5294 * t < calc.MAX_JOB_SECONDS

    def test_data_analyzer_in_game_reference(self):
        """Verified in game 2026-07-30. Data Analyzer I, NPC station, same skills:
        the client capped Runs at 2648.

        A second, independent confirmation of ceil — different base time (1800 s
        against Damage Control's 900) — but deliberately not of the exact-time
        basis: at an NPC station both bases predict 2648, so this measurement
        cannot distinguish them. That question rests on Damage Control I, where
        5295 was predicted by the exact basis alone.
        """
        t = calc.production_time(1800, te=20, industry=5, advanced_industry=5)
        assert round(t) == 979
        assert calc.max_runs_per_job(t) == 2648
        assert calc.max_runs_per_job(round(t)) == 2648      # bases agree here
        assert 2648 * t > calc.MAX_JOB_SECONDS             # ceil overshoots...
        assert 2647 * t < calc.MAX_JOB_SECONDS             # ...where floor fits

    def test_forum_worked_example(self):
        # 1h42m per run: ceil(2_592_000 / 6_120) = 424, and 424 runs is 30.03
        # days — the accepted maximum sits just over the limit, so ceil not floor
        assert calc.max_runs_per_job(6_120) == 424
        assert 424 * 6_120 > calc.MAX_JOB_SECONDS
        assert 423 * 6_120 < calc.MAX_JOB_SECONDS

    def test_exact_division_is_not_rounded_up(self):
        # a run time that divides 30 days evenly: that many runs fit exactly
        assert calc.max_runs_per_job(calc.MAX_JOB_SECONDS / 100) == 100

    def test_fast_item(self):
        assert calc.max_runs_per_job(131) == 19_787

    def test_single_run_over_30_days_is_exempt(self):
        # capitals/supers exceed the cap with one run and are allowed anyway
        assert calc.max_runs_per_job(3_916_800) == 1     # Erebus
        assert calc.max_runs_per_job(calc.MAX_JOB_SECONDS * 10) == 1

    def test_unknown_time(self):
        assert calc.max_runs_per_job(0) is None
        assert calc.max_runs_per_job(-5) is None


class TestVolumeWeightedPrice:
    def test_single_order(self):
        assert calc.volume_weighted_price([[10.0, 100]], 50) == pytest.approx(10.0)

    def test_walks_book(self):
        # 50 @ 10 + 50 @ 20 = 1500 / 100 = 15
        assert calc.volume_weighted_price([[10.0, 50], [20.0, 50]], 100) == pytest.approx(15.0)

    def test_insufficient_depth(self):
        assert calc.volume_weighted_price([[10.0, 10]], 100) is None

    def test_empty_book(self):
        assert calc.volume_weighted_price([], 1) is None

    def test_zero_quantity(self):
        assert calc.volume_weighted_price([[10.0, 10]], 0) is None


class TestPercentilePrice:
    def _hist(self, prices):
        return [{"average": p, "volume": 1} for p in prices]

    def test_empty(self):
        assert calc.percentile_price([], 5) is None

    def test_single_day(self):
        assert calc.percentile_price(self._hist([100.0]), 5) == pytest.approx(100.0)

    def test_median(self):
        assert calc.percentile_price(self._hist([10, 20, 30]), 50) == pytest.approx(20.0)

    def test_p5_interpolates(self):
        # 11 sorted values 0..100 step 10: p5 -> k=0.5 -> 5.0
        assert calc.percentile_price(self._hist(list(range(0, 101, 10))), 5) == pytest.approx(5.0)

    def test_uses_last_days_only(self):
        # old spike outside the 30-entry window is ignored
        hist = self._hist([1000.0] + [10.0] * 30)
        assert calc.percentile_price(hist, 95, days=30) == pytest.approx(10.0)

    def test_unsorted_input(self):
        assert calc.percentile_price(self._hist([30, 10, 20]), 0) == pytest.approx(10.0)


class TestAvgDailyVolume:
    TODAY = date(2026, 7, 29)   # ESI's freshest entry is then 2026-07-28

    def _hist(self, pairs):
        """pairs: [(days_before_today, volume), ...] oldest first."""
        return [
            {"date": (self.TODAY - timedelta(days=d)).isoformat(), "volume": v}
            for d, v in sorted(pairs, reverse=True)
        ]

    def test_dense_history(self):
        hist = self._hist([(d, 100) for d in range(1, 8)])
        assert calc.avg_daily_volume(hist, self.TODAY) == pytest.approx(100.0)

    def test_untraded_days_count_as_zero(self):
        # 700 units in a single day of the window is 100/day over 7 calendar
        # days, not 700/day as dividing by the entry count would give
        hist = self._hist([(3, 700)])
        assert calc.avg_daily_volume(hist, self.TODAY) == pytest.approx(100.0)

    def test_stale_item_decays_to_zero(self):
        # the headline bug: 7 entries spanning a year used to report 4.2/day
        hist = self._hist([(400 + 30 * i, 30) for i in range(7)])
        assert calc.avg_daily_volume(hist, self.TODAY) == 0.0

    def test_yesterday_counts_today_does_not(self):
        # ESI only publishes a day's aggregate after it closes in UTC
        assert calc.avg_daily_volume(self._hist([(1, 70)]), self.TODAY) == pytest.approx(10.0)
        assert calc.avg_daily_volume(self._hist([(0, 70)]), self.TODAY) == 0.0

    def test_window_edges(self):
        assert calc.avg_daily_volume(self._hist([(7, 70)]), self.TODAY) == pytest.approx(10.0)
        assert calc.avg_daily_volume(self._hist([(8, 70)]), self.TODAY) == 0.0

    def test_older_entries_excluded(self):
        hist = self._hist([(60, 10_000), (2, 70)])
        assert calc.avg_daily_volume(hist, self.TODAY) == pytest.approx(10.0)

    def test_empty_and_malformed(self):
        assert calc.avg_daily_volume([], self.TODAY) == 0.0
        assert calc.avg_daily_volume([{"volume": 5}], self.TODAY) == 0.0
        assert calc.avg_daily_volume([{"date": "not-a-date", "volume": 5}], self.TODAY) == 0.0

    def test_custom_window(self):
        hist = self._hist([(d, 10) for d in range(1, 31)])
        assert calc.avg_daily_volume(hist, self.TODAY, days=30) == pytest.approx(10.0)


class TestRealisticPrices:
    def test_sell_caps_wishful_ask(self):
        assert calc.realistic_sell_price(1000.0, 100.0) == pytest.approx(100.0)

    def test_sell_keeps_competitive_ask(self):
        assert calc.realistic_sell_price(90.0, 100.0) == pytest.approx(90.0)

    def test_sell_none_handling(self):
        assert calc.realistic_sell_price(None, 100.0) == pytest.approx(100.0)
        assert calc.realistic_sell_price(90.0, None) == pytest.approx(90.0)
        assert calc.realistic_sell_price(None, None) is None

    def test_buy_floors_lowball_bid(self):
        assert calc.realistic_buy_price(1.0, 50.0) == pytest.approx(50.0)

    def test_buy_keeps_competitive_bid(self):
        assert calc.realistic_buy_price(60.0, 50.0) == pytest.approx(60.0)

    def test_buy_none_handling(self):
        assert calc.realistic_buy_price(None, 50.0) == pytest.approx(50.0)
        assert calc.realistic_buy_price(60.0, None) == pytest.approx(60.0)
        assert calc.realistic_buy_price(None, None) is None


class TestJobMaterialCost:
    BOOK = {
        34: {"buy": [[4.0, 10**9]], "sell": [[5.0, 10**9]]},          # deep mineral
        58919: {"buy": [[29000.0, 50]], "sell": [[29010.0, 500]]},    # finished module input
    }

    def test_all_inputs_priced_from_book(self):
        # 100 tritanium @ 5 + 1 module @ 29010 — the module is not skipped
        mc_i, mc_o, unp = calc.job_material_cost(
            [(34, 100), (58919, 1)], 1, 0, 0, 0, self.BOOK)
        assert mc_i == pytest.approx(100 * 5.0 + 29010.0)
        assert mc_o == pytest.approx(100 * 4.0 + 29000.0)
        assert unp == []

    def test_missing_sell_orders_marks_unpriceable(self):
        book = {34: {"buy": [[4.0, 10**9]], "sell": []}}
        mc_i, mc_o, unp = calc.job_material_cost([(34, 100)], 1, 0, 0, 0, book)
        assert mc_i is None          # never silently 0
        assert mc_o == pytest.approx(400.0)
        assert unp == [34]

    def test_type_absent_from_book_is_unpriceable(self):
        mc_i, mc_o, unp = calc.job_material_cost([(999999, 10)], 1, 0, 0, 0, {})
        assert mc_i is None and mc_o is None
        assert unp == [999999]

    def test_insufficient_depth_is_unpriceable(self):
        book = {34: {"buy": [], "sell": [[5.0, 10]]}}
        _, _, unp = calc.job_material_cost([(34, 100)], 1, 0, 0, 0, book)
        assert unp == [34]

    def test_p95_floors_buy_orders(self):
        book = {34: {"buy": [[1.0, 10**9]], "sell": [[5.0, 10**9]]}}
        _, mc_o, _ = calc.job_material_cost(
            [(34, 100)], 1, 0, 0, 0, book, hist_p95={34: 4.5})
        assert mc_o == pytest.approx(450.0)


class TestMarketLimitedIph:
    # 100 units in 10 h = 10 units/h from the line; profit 1000 -> 10 ISK/unit
    LINE = dict(profit=1000.0, units=100, job_seconds=10 * 3600)

    def test_line_binds_when_market_is_deeper(self):
        # market absorbs 480/day = 20/h > 10/h from the line -> unchanged
        r = calc.market_limited_iph(**self.LINE, daily_volume=480)
        assert r == pytest.approx(100.0)                      # == profit / 10 h

    def test_market_binds_when_shallower(self):
        # 24/day = 1/h against 10/h producible: a tenth of the throughput
        r = calc.market_limited_iph(**self.LINE, daily_volume=24)
        assert r == pytest.approx(10.0)

    def test_equal_rates(self):
        r = calc.market_limited_iph(**self.LINE, daily_volume=240)
        assert r == pytest.approx(100.0)

    def test_zero_volume_is_zero_not_none(self):
        # nothing traded in the window: real information, not missing data
        assert calc.market_limited_iph(**self.LINE, daily_volume=0) == 0.0

    def test_volume_not_fetched_yet(self):
        assert calc.market_limited_iph(**self.LINE, daily_volume=None) is None

    def test_none_profit_propagates(self):
        assert calc.market_limited_iph(None, 100, 3600, 240) is None

    def test_loss_making_item_loses_less_when_throttled(self):
        # the point of rate x profit-per-unit over min() of two ISK/h: a slower
        # operation must lose LESS per hour, not be reported as worse
        fast = calc.market_limited_iph(-1000.0, 100, 10 * 3600, 480)
        slow = calc.market_limited_iph(-1000.0, 100, 10 * 3600, 24)
        assert fast == pytest.approx(-100.0)
        assert slow == pytest.approx(-10.0)
        assert slow > fast

    def test_degenerate_inputs(self):
        assert calc.market_limited_iph(1000.0, 0, 3600, 240) is None
        assert calc.market_limited_iph(1000.0, 100, 0, 240) is None

    def test_matches_hand_computed_live_case(self):
        # Large Vorton Projector I, Runs=100: 100 units in 32.6 h, profit
        # 30_520_838_281, volume 1.7/day -> market binds at 0.0708 units/h
        r = calc.market_limited_iph(30_520_838_281.0, 100, 32.6 * 3600, 1.7)
        assert r == pytest.approx(305_208_382.81 * (1.7 / 24), rel=1e-9)
        assert r < 22_000_000        # against 935M from the line-only figure


class TestScenario:
    def test_instant_both_sides(self):
        # cost 100 mats + 10 job; sell 1 unit at 200; tax 10%; no broker
        s = calc.scenario(100, 10, 200, 1, False, False, 0.03, 0.10, 3600)
        assert s.profit_per_run == pytest.approx(200 * 0.9 - 110)
        assert s.margin_pct == pytest.approx((180 - 110) / 110 * 100)
        assert s.isk_per_hour == pytest.approx(70.0)

    def test_broker_applied_on_both(self):
        s = calc.scenario(100, 0, 200, 1, True, True, 0.02, 0.0, 3600)
        # cost 100*1.02 = 102; revenue 200 - 200*0.02 = 196
        assert s.profit_per_run == pytest.approx(94.0)

    def test_none_propagates(self):
        s = calc.scenario(None, 10, 200, 1, False, False, 0.03, 0.1, 3600)
        assert s.profit_per_run is None and s.margin_pct is None and s.isk_per_hour is None
