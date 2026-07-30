"""State-mutation tests for app.py: history recording and settings merging.

Importing app builds the global State (reads data/settings.json, creates the
cache dir) but starts no server — the bootstrap thread only runs on the
FastAPI startup event.
"""
from datetime import date

import pytest
from fastapi import HTTPException

import app
import config


@pytest.fixture
def clean_state():
    """Snapshot and restore the bits of the global State these tests touch."""
    saved = (dict(app.S.hist_prices), dict(app.S.volumes),
             app.S.history_done, app.S.settings, list(app.S.rows), app.S.status)
    app.S.hist_prices.clear()
    app.S.volumes.clear()
    app.S.history_done = 0
    yield app.S
    app.S.hist_prices.clear()
    app.S.hist_prices.update(saved[0])
    app.S.volumes.clear()
    app.S.volumes.update(saved[1])
    app.S.history_done = saved[2]
    app.S.settings = saved[3]
    app.S.rows = saved[4]
    app.S.status = saved[5]


TODAY = date(2026, 7, 29)
HIST = [{"date": "2026-07-28", "volume": 700, "average": 10.0}]


class TestRecordHistory:
    def test_records_stats_and_volume(self, clean_state):
        app._record_history(34, HIST, {34}, TODAY)
        assert clean_state.volumes[34] == pytest.approx(100.0)
        assert clean_state.hist_prices[34]["p95"] == pytest.approx(10.0)
        assert clean_state.history_done == 1

    def test_volume_only_for_products(self, clean_state):
        app._record_history(34, HIST, set(), TODAY)   # 34 is a material here
        assert 34 not in clean_state.volumes
        assert clean_state.hist_prices[34]["p5"] == pytest.approx(10.0)

    def test_failed_fetch_keeps_cached_stats(self, clean_state):
        app._record_history(34, HIST, {34}, TODAY)
        app._record_history(34, None, {34}, TODAY)    # transient ESI failure
        assert clean_state.volumes[34] == pytest.approx(100.0)
        assert clean_state.hist_prices[34]["p95"] == pytest.approx(10.0)

    def test_failed_fetch_still_counts_as_done(self, clean_state):
        app._record_history(34, None, {34}, TODAY)
        assert clean_state.history_done == 1          # progress must not stall
        assert 34 not in clean_state.hist_prices

    def test_empty_history_is_recorded(self, clean_state):
        # [] is real information (never traded), unlike a failure
        app._record_history(34, [], {34}, TODAY)
        assert clean_state.volumes[34] == 0.0
        assert clean_state.hist_prices[34] == {"p5": None, "p95": None}


class TestPutSettingsMerge:
    @pytest.fixture(autouse=True)
    def _isolate_settings_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")

    def test_partial_put_keeps_blueprint_overrides(self, clean_state):
        clean_state.settings = config.Settings(
            runs=1, blueprint_overrides={"1234": {"me": 7}})
        out = app.put_settings({"runs": 5})
        assert out["runs"] == 5
        assert out["blueprint_overrides"] == {"1234": {"me": 7}}

    def test_stale_snapshot_cannot_erase_a_new_override(self, clean_state):
        """The settings modal reads its snapshot, an override is added while it
        is open, then Save posts only the modal's own fields."""
        clean_state.settings = config.Settings(blueprint_overrides={})
        snapshot_fields = {"accounting": 4, "structure_tax": 2.0}
        clean_state.settings.blueprint_overrides["1234"] = {"te": 12}
        out = app.put_settings(snapshot_fields)
        assert out["blueprint_overrides"] == {"1234": {"te": 12}}
        assert out["accounting"] == 4 and out["structure_tax"] == pytest.approx(2.0)

    def test_omitted_fields_keep_their_value(self, clean_state):
        clean_state.settings = config.Settings(accounting=2, runs=9, system_name="Sobaseki")
        out = app.put_settings({"industry": 3})
        assert out["accounting"] == 2 and out["runs"] == 9
        assert out["system_name"] == "Sobaseki"
        assert out["industry"] == 3

    def test_unknown_keys_ignored(self, clean_state):
        clean_state.settings = config.Settings()
        out = app.put_settings({"current_manufacturing_index": 0.05, "runs": 3})
        assert "current_manufacturing_index" not in out
        assert out["runs"] == 3

    def test_invalid_value_rejected_and_state_untouched(self, clean_state):
        clean_state.settings = config.Settings(accounting=5)
        with pytest.raises(HTTPException) as ei:
            app.put_settings({"accounting": 9})
        assert ei.value.status_code == 400
        assert clean_state.settings.accounting == 5


class TestItemsStayServedDuringRefresh:
    def test_rows_served_while_refreshing(self, clean_state):
        clean_state.rows = [{"type_id": 34, "name": "Tritanium"}]
        clean_state.status = "fetching_orders"
        out = app.items()
        assert out["rows"] == clean_state.rows
        assert out["stale"] is True

    def test_not_stale_when_ready(self, clean_state):
        clean_state.rows = [{"type_id": 34}]
        clean_state.status = "ready"
        assert app.items()["stale"] is False

    def test_cold_start_still_503s(self, clean_state):
        clean_state.rows = []
        clean_state.status = "loading"
        with pytest.raises(HTTPException) as ei:
            app.items()
        assert ei.value.status_code == 503


class TestFeesPreview:
    """/api/fees backs the live readout under the Standings section."""

    def test_max_skill_and_standings_land_exactly_on_the_floor(self):
        f = app.fees(broker_relations=5, faction_standing=10, corp_standing=10, accounting=5)
        assert f["broker_fee_pct"] == pytest.approx(1.0)      # 3 - 1.5 - 0.3 - 0.2
        assert f["broker_fee_uncapped_pct"] == pytest.approx(1.0)
        assert f["at_floor"] is True
        assert f["sales_tax_pct"] == pytest.approx(3.375)
        assert f["total_sell_pct"] == pytest.approx(4.375)

    def test_breakdown_adds_up(self):
        f = app.fees(broker_relations=3, faction_standing=5, corp_standing=2)
        total = (f["base_pct"] - f["from_skill_pct"]
                 - f["from_faction_pct"] - f["from_corp_pct"])
        assert total == pytest.approx(f["broker_fee_uncapped_pct"])
        assert f["at_floor"] is False

    def test_extra_standing_past_the_floor_buys_nothing(self):
        a = app.fees(broker_relations=5, faction_standing=50, corp_standing=50)
        b = app.fees(broker_relations=5, faction_standing=10, corp_standing=10)
        assert a["broker_fee_pct"] == pytest.approx(b["broker_fee_pct"])
        assert a["broker_fee_uncapped_pct"] < a["floor_pct"]
        assert a["at_floor"] and b["at_floor"]

    def test_standings_saving_is_after_the_floor(self):
        # BR0: 3% -> 2.5%, a real 0.5% saving
        assert app.fees(0, 10, 10)["standings_saving_pct"] == pytest.approx(0.5)
        # BR5 already sits at 1.5%; standings can only buy 0.5% before the floor
        assert app.fees(5, 50, 50)["standings_saving_pct"] == pytest.approx(0.5)

    def test_negative_standing_raises_the_fee(self):
        f = app.fees(broker_relations=0, faction_standing=-10, corp_standing=-10)
        assert f["broker_fee_pct"] == pytest.approx(3.5)
        assert f["at_floor"] is False

    def test_fractional_input_does_not_blow_up(self):
        # the preview runs on half-typed values and must never 422
        f = app.fees(broker_relations=4.5, accounting=2.5)
        assert f["broker_fee_pct"] == pytest.approx(1.65)
        assert f["sales_tax_pct"] == pytest.approx(7.5 * (1 - 0.11 * 2.5))

    def test_defaults_are_the_untrained_case(self):
        f = app.fees()
        assert f["broker_fee_pct"] == pytest.approx(3.0)
        assert f["sales_tax_pct"] == pytest.approx(7.5)


class TestBlueprintOverrideRollback:
    @pytest.fixture(autouse=True)
    def _isolate_settings_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")

    def test_rejected_edit_keeps_the_previous_override(self, clean_state):
        clean_state.settings = config.Settings(
            blueprint_overrides={"1234": {"me": 7, "te": 14}})
        with pytest.raises(HTTPException) as ei:
            app.set_blueprint_override(1234, {"me": 99})   # ME must be 0-10
        assert ei.value.status_code == 400
        assert clean_state.settings.blueprint_overrides == {"1234": {"me": 7, "te": 14}}

    def test_rejected_new_override_leaves_none_behind(self, clean_state):
        clean_state.settings = config.Settings(blueprint_overrides={})
        with pytest.raises(HTTPException):
            app.set_blueprint_override(1234, {"te": 99})
        assert clean_state.settings.blueprint_overrides == {}

    def test_valid_edit_replaces_and_clear_removes(self, clean_state):
        clean_state.settings = config.Settings(blueprint_overrides={"1234": {"me": 7}})
        assert app.set_blueprint_override(1234, {"me": 4})["override"] == {"me": 4}
        out = app.set_blueprint_override(1234, {"me": None, "te": None, "runs": None})
        assert out["override"] is None
        assert clean_state.settings.blueprint_overrides == {}
