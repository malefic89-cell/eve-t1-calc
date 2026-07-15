"""Regression tests against real SDE recipes (skipped if the SDE isn't
downloaded). Prices are synthetic — only the recipe structure is real."""
import pytest

import calc
import sde as sde_mod

pytestmark = pytest.mark.skipif(
    not sde_mod.sde_exists(), reason="SDE not downloaded (data/sde.sqlite)"
)


def _recipe(product_name: str):
    s = sde_mod.SDE()
    try:
        pid = s.conn.execute(
            "SELECT typeID FROM invTypes WHERE typeName = ?", (product_name,)
        ).fetchone()["typeID"]
        bp = s.conn.execute(
            "SELECT typeID FROM industryActivityProducts"
            " WHERE productTypeID = ? AND activityID = 1", (pid,)
        ).fetchone()["typeID"]
        return s.materials_for_blueprint(bp), s
    except TypeError:
        s.close()
        pytest.skip(f"{product_name} not found in this SDE version")


def test_conversion_recipe_prices_finished_module_input():
    """Compact Interdiction Nullifier consumes 1x Interdiction Nullifier I —
    a finished market module. Its production cost must include that module
    at its Jita sell price, i.e. cost >= the module's ask."""
    mats, s = _recipe("Compact Interdiction Nullifier")
    module_id = s.conn.execute(
        "SELECT typeID FROM invTypes WHERE typeName = ?",
        ("Interdiction Nullifier I",),
    ).fetchone()["typeID"]
    s.close()
    mat_ids = {m.type_id for m in mats}
    assert module_id in mat_ids, "recipe no longer consumes the module"

    ask = 29010.0  # synthetic Jita ask for the module; other inputs at 1 ISK
    book = {
        m.type_id: {"buy": [], "sell": [[ask if m.type_id == module_id else 1.0, 10**9]]}
        for m in mats
    }
    mc_i, _, unpriceable = calc.job_material_cost(
        [(m.type_id, m.base_qty) for m in mats], 1, 10, 1.0, 2.0, book
    )
    assert unpriceable == []
    assert mc_i is not None and mc_i >= ask

    # and if the module has no sell orders, the row must become unpriceable,
    # not silently priced at 0
    book[module_id]["sell"] = []
    mc_i, _, unpriceable = calc.job_material_cost(
        [(m.type_id, m.base_qty) for m in mats], 1, 10, 1.0, 2.0, book
    )
    assert mc_i is None
    assert module_id in unpriceable
