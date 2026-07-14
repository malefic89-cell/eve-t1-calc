import math

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
        assert calc.broker_fee_rate(5, 50.0, 50.0) == pytest.approx(0.005)

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
