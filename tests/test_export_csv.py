"""Testes do export CSV streaming e cleanup."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import yaml

from txt2sql.config import ExportConfig, load_config
from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.export_csv import (
    build_export_url,
    cleanup_expired_exports,
    export_denormalized_csv,
)


def test_build_export_url() -> None:
    assert (
        build_export_url("https://app.example/exports/", "a.csv")
        == "https://app.example/exports/a.csv"
    )
    assert (
        build_export_url("https://app.example/exports", "a.csv")
        == "https://app.example/exports/a.csv"
    )


def test_load_export_config(tmp_path: Path) -> None:
    raw = {
        "databases": [{"id": "db", "connection_string": "sqlite:///:memory:"}],
        "tables": [{"id": "t", "database": "db", "name": "t"}],
        "agent": {
            "export": {
                "enabled": True,
                "dir": str(tmp_path / "out"),
                "base_url": "http://localhost/files",
                "ttl_seconds": 3600,
                "delimiter": ";",
                "max_rows": 1000,
            }
        },
    }
    p = tmp_path / "c.yaml"
    p.write_text(yaml.dump(raw), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.export is not None
    assert cfg.export.enabled is True
    assert cfg.export.delimiter == ";"
    assert cfg.export.max_rows == 1000
    assert cfg.export.base_url == "http://localhost/files"


def test_load_export_config_default_disabled(tmp_path: Path) -> None:
    raw = {
        "databases": [{"id": "db", "connection_string": "sqlite:///:memory:"}],
        "tables": [{"id": "t", "database": "db", "name": "t"}],
    }
    p = tmp_path / "c.yaml"
    p.write_text(yaml.dump(raw), encoding="utf-8")
    assert load_config(p).export.enabled is False


def test_export_config_enabled_requires_dir_and_base_url() -> None:
    with pytest.raises(ValueError, match="dir"):
        ExportConfig(enabled=True, dir="", base_url="http://x")
    with pytest.raises(ValueError, match="base_url"):
        ExportConfig(enabled=True, dir="/tmp", base_url="")


def test_export_denormalized_csv_streaming(tmp_path: Path) -> None:
    session = DuckDBSession()
    session._conn.execute(
        "CREATE TABLE recebiveis AS SELECT * FROM (VALUES ('1', 10.0), ('2', 20.0)) t(cnpj, valor)"
    )
    session._conn.execute(
        "CREATE TABLE clientes AS SELECT * FROM (VALUES ('1', 'A'), ('2', 'B')) t(cnpj, razao)"
    )
    export_cfg = ExportConfig(
        enabled=True,
        dir=str(tmp_path / "exports"),
        base_url="http://localhost/exports",
        delimiter=";",
        max_rows=100,
    )
    sql = (
        'SELECT c.razao, r.cnpj, r.valor FROM "recebiveis" r '
        'JOIN "clientes" c ON c.cnpj = r.cnpj'
    )
    # Spy: execute (fetchall path) não deve ser usado para o dump
    original_execute = session.execute
    calls: list[str] = []

    def tracking_execute(q: str):
        calls.append(q)
        return original_execute(q)

    session.execute = tracking_execute  # type: ignore[method-assign]

    result = export_denormalized_csv(
        session=session,
        select_sql=sql,
        config=export_cfg,
        thread_id="t1",
    )
    assert result.path.exists()
    assert result.url.startswith("http://localhost/exports/")
    assert result.row_count == 2
    assert not result.truncated
    text = result.path.read_text(encoding="utf-8")
    assert ";" in text
    assert "razao" in text.lower() or "A" in text
    # Dump não passa por execute() com SELECT completo sem COUNT/COPY
    dump_selects = [c for c in calls if c.strip().upper().startswith("SELECT") and "COUNT" not in c.upper()]
    assert not dump_selects
    session.close()


def test_export_truncates_at_max_rows(tmp_path: Path) -> None:
    session = DuckDBSession()
    session._conn.execute(
        "CREATE TABLE t AS SELECT i AS id FROM range(10) r(i)"
    )
    cfg = ExportConfig(
        enabled=True,
        dir=str(tmp_path / "e"),
        base_url="http://x/e",
        max_rows=3,
    )
    result = export_denormalized_csv(
        session=session,
        select_sql='SELECT * FROM "t"',
        config=cfg,
        thread_id="th",
    )
    assert result.truncated
    assert result.row_count == 3
    lines = [ln for ln in result.path.read_text().splitlines() if ln.strip()]
    # header + 3 rows
    assert len(lines) == 4
    session.close()


def test_cleanup_expired_exports(tmp_path: Path) -> None:
    d = tmp_path / "exports"
    d.mkdir()
    old = d / "old.csv"
    new = d / "new.csv"
    old.write_text("a", encoding="utf-8")
    new.write_text("b", encoding="utf-8")
    old_mtime = time.time() - 10_000
    os.utime(old, (old_mtime, old_mtime))
    removed = cleanup_expired_exports(d, ttl_seconds=60)
    assert removed == 1
    assert not old.exists()
    assert new.exists()
