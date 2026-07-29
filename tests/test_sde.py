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


def test_concurrent_queries_are_serialized(sde):
    """/api/systems runs in FastAPI's threadpool while bootstrap queries the
    same connection; interleaved cursors would corrupt or raise."""
    def work(i):
        assert sde.type_name(34) == "Tritanium"
        return len(sde.search_systems("Jita")) + len(sde.materials_for_blueprint(681))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(work, range(64)))
    assert len(set(results)) == 1, "concurrent queries returned inconsistent results"
