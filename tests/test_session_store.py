"""Testes do store de sessões DuckDB por thread_id."""

from __future__ import annotations

from txt2sql.db.session_store import DuckDBSessionStore


def test_get_twice_same_thread_persists(tmp_path) -> None:
    store = DuckDBSessionStore(tmp_path)
    s1 = store.get("thread-a")
    s1._conn.execute("CREATE TABLE t (id INTEGER)")
    s1._conn.execute("INSERT INTO t VALUES (1)")
    store.close("thread-a")
    s2 = store.get("thread-a")
    rows = s2._conn.execute("SELECT id FROM t").fetchall()
    assert rows == [(1,)]


def test_different_threads_isolated(tmp_path) -> None:
    store = DuckDBSessionStore(tmp_path)
    s_a = store.get("thread-a")
    s_a._conn.execute("CREATE TABLE t (id INTEGER)")
    s_a._conn.execute("INSERT INTO t VALUES (1)")
    s_b = store.get("thread-b")
    tables = s_b._conn.execute("SHOW TABLES").fetchall()
    assert tables == []
    store.close("thread-a")
    store.close("thread-b")


def test_get_returns_cached_session(tmp_path) -> None:
    store = DuckDBSessionStore(tmp_path)
    s1 = store.get("thread-a")
    s2 = store.get("thread-a")
    assert s1 is s2


def test_safe_id_sanitizes_thread_id(tmp_path) -> None:
    store = DuckDBSessionStore(tmp_path)
    store.get("thread/a:b")
    assert (tmp_path / "thread_a_b.duckdb").is_file()
    store.close("thread/a:b")
