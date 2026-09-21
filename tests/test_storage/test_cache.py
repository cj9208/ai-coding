from __future__ import annotations

from pathlib import Path

import pytest

from storage import SqliteCache


@pytest.fixture()
def cache(tmp_path: Path) -> SqliteCache:
    return SqliteCache(tmp_path / "cache.db")


def test_put_and_get(cache: SqliteCache):
    cache.put("k1", "value1", tag="v1")
    assert cache.get("k1", tag="v1") == "value1"


def test_get_missing_key_returns_none(cache: SqliteCache):
    assert cache.get("nonexistent", tag="v1") is None


def test_tag_mismatch_is_a_miss(cache: SqliteCache):
    cache.put("k1", "value1", tag="v1")
    assert cache.get("k1", tag="v2") is None


def test_replace_existing_key(cache: SqliteCache):
    cache.put("k1", "old", tag="v1")
    cache.put("k1", "new", tag="v1")
    assert cache.get("k1", tag="v1") == "new"


def test_same_key_new_tag_replaces(cache: SqliteCache):
    cache.put("k1", "value_v1", tag="v1")
    cache.put("k1", "value_v2", tag="v2")
    assert cache.get("k1", tag="v1") is None
    assert cache.get("k1", tag="v2") == "value_v2"


def test_clear_by_tag(cache: SqliteCache):
    cache.put("k1", "a", tag="v1")
    cache.put("k2", "b", tag="v1")
    cache.put("k3", "c", tag="v2")
    deleted = cache.clear(tag="v1")
    assert deleted == 2
    assert cache.get("k1", tag="v1") is None
    assert cache.get("k3", tag="v2") == "c"


def test_clear_all(cache: SqliteCache):
    cache.put("k1", "a", tag="v1")
    cache.put("k2", "b", tag="v2")
    deleted = cache.clear()
    assert deleted == 2
    assert cache.get("k1", tag="v1") is None
    assert cache.get("k2", tag="v2") is None


def test_persistence_across_instances(tmp_path: Path):
    db = tmp_path / "cache.db"
    c1 = SqliteCache(db)
    c1.put("k1", "persistent", tag="v1")
    c1.close()

    c2 = SqliteCache(db)
    assert c2.get("k1", tag="v1") == "persistent"
    c2.close()


def test_creates_parent_directories(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "c" / "cache.db"
    cache = SqliteCache(nested)
    cache.put("k", "v", tag="t")
    assert cache.get("k", tag="t") == "v"
    cache.close()
