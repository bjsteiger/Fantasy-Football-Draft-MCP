"""The parquet cache layer, tested offline with builders that never touch the network."""
import os
import time

import pandas as pd
import pytest

from ffdraft import sources


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "CACHE_DIR", tmp_path)
    sources.clear_memory_cache()
    yield tmp_path
    sources.clear_memory_cache()


def _backdate(path, days: float) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


class TestCachedFallback:
    """A stale parquet beats no data at all.

    The module promises a draft never waits on the network. Before the
    fallback, the moment a parquet aged past its refresh window a failed
    rebuild -- the network being down on draft day -- crashed every tool,
    with a perfectly usable copy sitting on disk.
    """

    def test_builds_and_writes_through_on_first_call(self, cache_dir):
        df = pd.DataFrame({"a": [1, 2]})
        out = sources._cached("t1", lambda: df)
        assert out.equals(df)
        assert (cache_dir / "t1.parquet").exists()

    def test_fresh_parquet_is_served_without_rebuilding(self, cache_dir):
        sources._cached("t2", lambda: pd.DataFrame({"a": [1]}))
        sources.clear_memory_cache()

        def explode():
            raise AssertionError("fresh cache must not rebuild")

        out = sources._cached("t2", explode)
        assert list(out["a"]) == [1]

    def test_stale_parquet_survives_a_failed_rebuild(self, cache_dir):
        sources._cached("t3", lambda: pd.DataFrame({"a": [7]}))
        sources.clear_memory_cache()
        _backdate(cache_dir / "t3.parquet", days=30)

        def network_down():
            raise ConnectionError("no route to nflverse")

        out = sources._cached("t3", network_down, max_age_days=7.0)
        assert list(out["a"]) == [7]

    def test_no_cache_and_a_failed_build_still_raises(self, cache_dir):
        def network_down():
            raise ConnectionError("no route to nflverse")

        with pytest.raises(ConnectionError):
            sources._cached("t4", network_down)

    def test_stale_parquet_is_replaced_when_the_rebuild_works(self, cache_dir):
        sources._cached("t5", lambda: pd.DataFrame({"a": [1]}))
        sources.clear_memory_cache()
        _backdate(cache_dir / "t5.parquet", days=30)

        out = sources._cached("t5", lambda: pd.DataFrame({"a": [2]}), max_age_days=7.0)
        assert list(out["a"]) == [2]
        sources.clear_memory_cache()
        assert list(sources._cached("t5", lambda: None)["a"]) == [2]
