"""Testes do módulo fan_in (fan-in determinístico de shards no DuckDB)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, text

from txt2sql.config import (
    AgentConfig,
    DatabaseConfig,
    DuckDBConfig,
    ShardingConfig,
    ShardResult,
    TableConfig,
    load_config,
)
from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.db.fan_in import FanInResult, fan_in
from txt2sql.db.registry import DatabaseRegistry
from txt2sql.shard_routing import ShardBinding


def test_load_config_max_shard_discriminators(tmp_path: Path) -> None:
    raw = {
        "databases": [{"id": "db", "connection_string": "sqlite:///:memory:"}],
        "tables": [{"id": "t", "database": "db", "name": "t"}],
        "agent": {"max_shard_discriminators": 7},
    }
    p = tmp_path / "c.yaml"
    p.write_text(yaml.dump(raw), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.max_shard_discriminators == 7


def test_load_config_max_shard_discriminators_default(tmp_path: Path) -> None:
    raw = {
        "databases": [{"id": "db", "connection_string": "sqlite:///:memory:"}],
        "tables": [{"id": "t", "database": "db", "name": "t"}],
    }
    p = tmp_path / "c.yaml"
    p.write_text(yaml.dump(raw), encoding="utf-8")
    assert load_config(p).max_shard_discriminators == 20


def _sharded_table() -> TableConfig:
    return TableConfig(
        id="recebiveis",
        database="db_a",
        name="recebiveis",
        sharding=ShardingConfig(
            discriminator_column="cnpj",
            resolver="tests.test_fan_in:_resolver_fn",
        ),
        duckdb=DuckDBConfig(enabled=True, trigger="aggregation", fetch_limit=100_000),
        columns=[],
    )


def _resolver_fn(v: str) -> ShardResult:
    if v.startswith("1"):
        return ShardResult(database_id="db_a", table_name="rec_a")
    return ShardResult(database_id="db_b", table_name="rec_b")


def _build_registry() -> tuple[DatabaseRegistry, AgentConfig]:
    eng_a = create_engine("sqlite:///:memory:")
    eng_b = create_engine("sqlite:///:memory:")
    with eng_a.begin() as c:
        c.execute(text("CREATE TABLE rec_a (cnpj TEXT, valor REAL)"))
        c.execute(text("INSERT INTO rec_a VALUES ('111', 10.0), ('122', 15.0), ('199', 99.0)"))
    with eng_b.begin() as c:
        c.execute(text("CREATE TABLE rec_b (cnpj TEXT, valor REAL)"))
        c.execute(text("INSERT INTO rec_b VALUES ('222', 20.0), ('333', 30.0)"))

    table = _sharded_table()
    config = AgentConfig(
        databases=[
            DatabaseConfig(id="db_a", connection_string="sqlite:///:memory:"),
            DatabaseConfig(id="db_b", connection_string="sqlite:///:memory:"),
        ],
        tables=[table],
        override_connections={},
    )
    registry = DatabaseRegistry.__new__(DatabaseRegistry)
    registry._engines = {"db_a": eng_a, "db_b": eng_b}  # type: ignore[attr-defined]
    registry._inspection_engines = {"db_a": eng_a, "db_b": eng_b}  # type: ignore[attr-defined]
    registry._config = config  # type: ignore[attr-defined]
    return registry, config


def _bindings(values: list[str]) -> list[ShardBinding]:
    """Constrói ShardBindings usando o resolver de teste."""
    result = []
    for v in values:
        sr = _resolver_fn(v)
        result.append(
            ShardBinding(
                table_id="recebiveis",
                database_id=sr.database_id,
                physical_table=sr.table_name,
                discriminator_value=v,
            )
        )
    return result


def test_fan_in_groups_and_filters() -> None:
    registry, config = _build_registry()
    table = config.get_table("recebiveis")
    session = DuckDBSession()
    try:
        result = fan_in(
            session=session,
            table=table,
            registry=registry,
            bindings=_bindings(["111", "122", "222"]),
        )
        assert isinstance(result, FanInResult)
        assert result.table_id == "recebiveis"
        assert result.row_count == 3
        assert set(result.physical_tables) == {"rec_a", "rec_b"}

        rows = session.execute(
            "SELECT cnpj, SUM(valor) AS s FROM recebiveis GROUP BY cnpj ORDER BY cnpj"
        )
        assert [(r["cnpj"], r["s"]) for r in rows] == [
            ("111", 10.0),
            ("122", 15.0),
            ("222", 20.0),
        ]
        # 199 está no mesmo físico que 111/122 mas não foi pedido — não deve aparecer
        assert (
            session.execute("SELECT COUNT(*) AS c FROM recebiveis WHERE cnpj = '199'")[0]["c"] == 0
        )
    finally:
        session.close()


def test_fan_in_returns_correct_row_count() -> None:
    registry, config = _build_registry()
    table = config.get_table("recebiveis")
    session = DuckDBSession()
    try:
        result = fan_in(
            session=session,
            table=table,
            registry=registry,
            bindings=_bindings(["111", "222", "333"]),
        )
        assert result.row_count == 3
    finally:
        session.close()


def test_fan_in_rejects_missing_physical_table() -> None:
    registry, config = _build_registry()
    table = config.get_table("recebiveis")
    session = DuckDBSession()
    try:
        bad_bindings = [
            ShardBinding(
                table_id="recebiveis",
                database_id="db_a",
                physical_table="rec_missing",
                discriminator_value="111",
            ),
            ShardBinding(
                table_id="recebiveis",
                database_id="db_a",
                physical_table="rec_missing",
                discriminator_value="122",
            ),
        ]
        with pytest.raises(ValueError, match="inexistente"):
            fan_in(
                session=session,
                table=table,
                registry=registry,
                bindings=bad_bindings,
            )
    finally:
        session.close()


def test_fan_in_single_physical_group() -> None:
    """Bindings que mapeiam para um único físico (todos no mesmo banco)."""
    registry, config = _build_registry()
    table = config.get_table("recebiveis")
    session = DuckDBSession()
    try:
        result = fan_in(
            session=session,
            table=table,
            registry=registry,
            bindings=_bindings(["111", "122"]),
        )
        assert result.row_count == 2
        assert result.physical_tables == ["rec_a"]
    finally:
        session.close()


def test_prompt_does_not_mention_react_tools() -> None:
    """Prompt do dual-path não deve instruir sobre tools resolve_shard/materialize."""
    from txt2sql.prompts import Txt2SqlPromptBuilder

    config = AgentConfig(
        databases=[DatabaseConfig(id="db_a", connection_string="sqlite:///:memory:")],
        tables=[_sharded_table()],
    )
    prompt = Txt2SqlPromptBuilder(config).build()
    assert "resolve_shard" not in prompt
    assert "materialize_sharded_table" not in prompt
    assert "discriminador" in prompt
