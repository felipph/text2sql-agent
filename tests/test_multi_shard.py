"""Testes do fan-in multi-shard."""

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
from txt2sql.db.multi_shard import (
    build_in_filter,
    materialize_sharded_values,
)
from txt2sql.db.registry import DatabaseRegistry
from txt2sql.db.shard import ShardResolver


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


def test_build_in_filter_escapes_quotes() -> None:
    assert build_in_filter("cnpj", ["a'b", "c"]) == "cnpj IN ('a''b', 'c')"


def _resolver_fn(v: str) -> ShardResult:
    if v.startswith("1"):
        return ShardResult(database_id="db_a", table_name="rec_a")
    return ShardResult(database_id="db_b", table_name="rec_b")


def _sharded_table() -> TableConfig:
    return TableConfig(
        id="recebiveis",
        database="db_a",
        name="recebiveis",
        sharding=ShardingConfig(
            discriminator_column="cnpj",
            resolver="tests.test_multi_shard:_resolver_fn",
        ),
        duckdb=DuckDBConfig(enabled=True, trigger="aggregation", fetch_limit=100_000),
        columns=[],
    )


def _build_registry_and_resolver() -> tuple[DatabaseRegistry, ShardResolver, AgentConfig]:
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
    # Registry real exigiria URLs; injetamos engines via monkey no teste.
    registry = DatabaseRegistry.__new__(DatabaseRegistry)
    registry._engines = {"db_a": eng_a, "db_b": eng_b}  # type: ignore[attr-defined]
    registry._config = config  # type: ignore[attr-defined]

    resolver = ShardResolver.__new__(ShardResolver)
    resolver._config = config  # type: ignore[attr-defined]
    resolver._registry = registry  # type: ignore[attr-defined]
    resolver._resolvers = {"recebiveis": _resolver_fn}  # type: ignore[attr-defined]
    return registry, resolver, config


def test_materialize_sharded_values_groups_and_filters() -> None:
    registry, resolver, config = _build_registry_and_resolver()
    table = config.get_table("recebiveis")
    session = DuckDBSession()
    try:
        result = materialize_sharded_values(
            table=table,
            values=["111", "122", "222"],
            max_discriminators=20,
            resolver=resolver,
            registry=registry,
            session=session,
        )
        assert result.truncated is False
        assert set(result.materialized_values) == {"111", "122", "222"}
        rows = session.execute(
            "SELECT cnpj, SUM(valor) AS s FROM recebiveis GROUP BY cnpj ORDER BY cnpj"
        )
        assert [(r["cnpj"], r["s"]) for r in rows] == [
            ("111", 10.0),
            ("122", 15.0),
            ("222", 20.0),
        ]
        # 199 está no mesmo físico que 111/122 mas não foi pedido
        assert (
            session.execute("SELECT COUNT(*) AS c FROM recebiveis WHERE cnpj = '199'")[0]["c"] == 0
        )
    finally:
        session.close()


def test_materialize_sharded_values_truncates() -> None:
    registry, resolver, config = _build_registry_and_resolver()
    table = config.get_table("recebiveis")
    session = DuckDBSession()
    try:
        result = materialize_sharded_values(
            table=table,
            values=["111", "222", "333"],
            max_discriminators=2,
            resolver=resolver,
            registry=registry,
            session=session,
        )
        assert result.truncated is True
        assert result.omitted_count == 1
        assert result.materialized_values == ["111", "222"]
        assert "333" not in {
            r["cnpj"] for r in session.execute("SELECT DISTINCT cnpj FROM recebiveis")
        }
    finally:
        session.close()


def test_materialize_sharded_values_rejects_empty() -> None:
    registry, resolver, config = _build_registry_and_resolver()
    session = DuckDBSession()
    try:
        with pytest.raises(ValueError, match="vazia"):
            materialize_sharded_values(
                table=config.get_table("recebiveis"),
                values=[],
                max_discriminators=20,
                resolver=resolver,
                registry=registry,
                session=session,
            )
    finally:
        session.close()


def test_materialize_sharded_values_rejects_single() -> None:
    registry, resolver, config = _build_registry_and_resolver()
    session = DuckDBSession()
    try:
        with pytest.raises(ValueError, match="resolve_shard"):
            materialize_sharded_values(
                table=config.get_table("recebiveis"),
                values=["111"],
                max_discriminators=20,
                resolver=resolver,
                registry=registry,
                session=session,
            )
    finally:
        session.close()


def test_materialize_rejects_missing_physical_table() -> None:
    registry, resolver, config = _build_registry_and_resolver()

    def _bad_resolver(v: str) -> ShardResult:
        return ShardResult(database_id="db_a", table_name="rec_missing")

    resolver._resolvers = {"recebiveis": _bad_resolver}  # type: ignore[attr-defined]
    session = DuckDBSession()
    try:
        with pytest.raises(ValueError, match="inexistente"):
            materialize_sharded_values(
                table=config.get_table("recebiveis"),
                values=["111", "122"],
                max_discriminators=20,
                resolver=resolver,
                registry=registry,
                session=session,
            )
    finally:
        session.close()


def test_prompt_mentions_materialize_sharded_table() -> None:
    from txt2sql.prompts import Txt2SqlPromptBuilder

    config = AgentConfig(
        databases=[
            DatabaseConfig(id="db_a", connection_string="sqlite:///:memory:"),
        ],
        tables=[_sharded_table()],
    )
    prompt = Txt2SqlPromptBuilder(config).build()
    assert "materialize_sharded_table" in prompt
    assert "nome lógico" in prompt.lower()
    assert "Receita quando a pergunta NÃO traz o discriminador" in prompt
    assert "liste os discriminadores" in prompt.lower() or "SELECT cnpj" in prompt
