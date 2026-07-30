"""SDE query surface, incl. the lock that serializes the shared connection.

Skipped unless the SDE has been downloaded.
"""
import concurrent.futures

import pytest

import sde as sde_mod

pytestmark = pytest.mark.skipif(
    not sde_mod.sde_exists(), reason="SDE not downloaded (data/sde.sqlite)"
)


@pytest.fixture(scope="module")
def sde():
    s = sde_mod.SDE()
    yield s
    s.close()


def test_search_systems(sde):
    hits = sde.search_systems("Sobaseki")
    assert any(h["name"] == "Sobaseki" for h in hits)
    assert all({"system_id", "name", "security"} <= h.keys() for h in hits)


def test_search_systems_respects_limit(sde):
    assert len(sde.search_systems("a", limit=3)) == 3


def test_type_name_hit_and_miss(sde):
    assert sde.type_name(34) == "Tritanium"
    assert sde.type_name(-1) is None


@pytest.fixture(scope="module")
def prods(sde):
    return sde.manufacturable_t1_products()


class TestRigCategories:
    """Rigs are lifted out of Module into one category per size."""

    def test_all_four_sizes_present(self, prods):
        cats = {p.category_name for p in prods}
        for size in ("Small", "Medium", "Large", "Capital"):
            assert f"Rigs ({size})" in cats

    def test_no_rig_left_in_module(self, prods):
        stragglers = [p for p in prods
                      if p.category_name == "Module"
                      and p.group_name.startswith("Rig ")]
        assert stragglers == []

    def test_nothing_but_rigs_lands_in_a_rig_category(self, prods):
        """Ships carry rigSize too, to say which size they accept, so keying on
        the attribute alone would file every frigate under Rigs (Small)."""
        intruders = [p.name for p in prods
                     if p.category_name.startswith("Rigs (")
                     and not p.group_name.startswith("Rig ")]
        assert intruders == []

    def test_a_frigate_stays_a_ship(self, prods):
        rifter = next((p for p in prods if p.name == "Rifter"), None)
        if rifter is None:
            pytest.skip("Rifter not in this SDE")
        assert rifter.category_name == "Ship"

    def test_size_matches_the_name_prefix(self, prods):
        """The rigSize attribute and the name prefix agree for every rig; if the
        SDE ever disagrees, the categories are the thing to distrust."""
        mismatched = [
            p.name for p in prods if p.category_name.startswith("Rigs (")
            and not p.name.startswith(p.category_name[6:-1] + " ")
        ]
        assert mismatched == []

    def test_group_names_are_untouched(self, prods):
        rigs = [p for p in prods if p.category_name.startswith("Rigs (")]
        assert rigs, "no rigs found at all"
        assert all(p.group_name.startswith("Rig ") for p in rigs)


def test_concurrent_queries_are_serialized(sde):
    """/api/systems runs in FastAPI's threadpool while bootstrap queries the
    same connection; interleaved cursors would corrupt or raise."""
    def work(i):
        assert sde.type_name(34) == "Tritanium"
        return len(sde.search_systems("Jita")) + len(sde.materials_for_blueprint(681))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(work, range(64)))
    assert len(set(results)) == 1, "concurrent queries returned inconsistent results"
