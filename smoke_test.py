"""Smoke test funcional (sem LLM) das camadas de dados da lib txt2sql.

Usa SQLite in-memory compartilhado para exercitar: load_config, DatabaseRegistry,
guardrail read-only, SchemaLoader (discovery + declarativo), resolve_routing e a
camada DuckDB (materialização + execução analítica).
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from txt2sql.config import (
    AgentConfig,
    ColumnConfig,
    DatabaseConfig,
    DuckDBConfig,
    ShardingConfig,
    TableConfig,
    load_config,
)
from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.db.registry import DatabaseRegistry
from txt2sql.db.schema import SchemaLoader
from txt2sql.guardrail import ReadOnlyViolationError, validate_sql
from txt2sql.intent import FilterClause, IntentPlan, MetricClause
from txt2sql.prompts import Txt2SqlPromptBuilder
from txt2sql.shard_routing import ClarifyNeeded, resolve_routing

FAILS = 0


def check(name: str, cond: bool) -> None:
    global FAILS
    status = "OK " if cond else "FALHOU"
    if not cond:
        FAILS += 1
    print(f"[{status}] {name}")


# --------------------------------------------------------------------------- #
# 1. Guardrail
# --------------------------------------------------------------------------- #
print("== Guardrail ==")
check("SELECT válido passa", validate_sql("SELECT id FROM t") == "SELECT id FROM t")
for bad in [
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET x=1",
    "DELETE FROM t",
    "DROP TABLE t",
    "SELECT * FROM t; DROP TABLE t",
    "SELECT id INTO novo FROM t",
    "EXEC sp_who",
    "WITH c AS (DELETE FROM t RETURNING *) SELECT * FROM c",
]:
    try:
        validate_sql(bad)
        check(f"rejeita: {bad[:35]}", False)
    except ReadOnlyViolationError:
        check(f"rejeita: {bad[:35]}", True)

# allowlist
try:
    validate_sql("SELECT a FROM permitida", allowed_tables=["permitida"])
    check("allowlist permite tabela no escopo", True)
except ReadOnlyViolationError:
    check("allowlist permite tabela no escopo", False)
try:
    validate_sql("SELECT a FROM proibida", allowed_tables=["permitida"])
    check("allowlist rejeita tabela fora do escopo", False)
except ReadOnlyViolationError:
    check("allowlist rejeita tabela fora do escopo", True)


# --------------------------------------------------------------------------- #
# 2. Registry + Schema discovery (SQLite via arquivo temporário)
# --------------------------------------------------------------------------- #
print("\n== Registry + Schema discovery ==")
tmpdir = tempfile.mkdtemp()
main_db = Path(tmpdir) / "main.db"
con = sqlite3.connect(main_db)
con.executescript(
    """
    CREATE TABLE clientes (cnpj TEXT, nome TEXT);
    INSERT INTO clientes VALUES ('11222333000181', 'ACME'), ('40000000000100', 'Beta');
    """
)
con.commit()
con.close()

cfg = AgentConfig(
    databases=[DatabaseConfig(id="db_main", connection_string=f"sqlite:///{main_db}")],
    tables=[TableConfig(id="clientes", database="db_main", name="clientes")],
)
registry = DatabaseRegistry(cfg)
loader = SchemaLoader(cfg, registry)
info = loader.get_table_info("clientes")
check("discovery lista coluna 'cnpj'", "cnpj" in info)
check("discovery inclui amostra", "ACME" in info)

# guardrail no engine bloqueia escrita
try:
    registry.execute("db_main", "DELETE FROM clientes")
    check("engine read-only bloqueia DELETE", False)
except ReadOnlyViolationError:
    check("engine read-only bloqueia DELETE", True)

rows = registry.execute("db_main", "SELECT cnpj FROM clientes ORDER BY cnpj")
check("engine executa SELECT", len(rows) == 2)


# --------------------------------------------------------------------------- #
# 3. Schema declarativo
# --------------------------------------------------------------------------- #
print("\n== Schema declarativo ==")
cfg2 = AgentConfig(
    databases=[DatabaseConfig(id="db_main", connection_string=f"sqlite:///{main_db}")],
    tables=[
        TableConfig(
            id="recebiveis",
            database="db_main",
            name="recebiveis",
            columns=[
                ColumnConfig(name="cnpj", type="VARCHAR", description="CNPJ do titular"),
                ColumnConfig(name="valor", type="NUMERIC", description="Valor em BRL"),
            ],
        )
    ],
)
loader2 = SchemaLoader(cfg2, DatabaseRegistry(cfg2))
info2 = loader2.get_table_info("recebiveis")
check("declarativo usa description do YAML", "CNPJ do titular" in info2)
check("declarativo não faz discovery (tabela inexistente OK)", "recebiveis" in info2)


# --------------------------------------------------------------------------- #
# 4. Sharding
# --------------------------------------------------------------------------- #
print("\n== Sharding ==")
# cria 3 shards com engine
shard_dbs = {}
for i in (1, 2, 3):
    p = Path(tmpdir) / f"shard{i}.db"
    c = sqlite3.connect(p)
    c.executescript(
        f"CREATE TABLE recebiveis_{i:03d} (cnpj TEXT, valor REAL);"
        f"INSERT INTO recebiveis_{i:03d} VALUES ('x', {i*10.0});"
    )
    c.commit()
    c.close()
    shard_dbs[i] = p

cfg3 = AgentConfig(
    databases=[
        DatabaseConfig(id="db_main", connection_string=f"sqlite:///{main_db}"),
        DatabaseConfig(id="db_shard_1", connection_string=f"sqlite:///{shard_dbs[1]}"),
        DatabaseConfig(id="db_shard_2", connection_string=f"sqlite:///{shard_dbs[2]}"),
        DatabaseConfig(id="db_shard_3", connection_string=f"sqlite:///{shard_dbs[3]}"),
    ],
    tables=[
        TableConfig(
            id="recebiveis",
            database="db_main",
            name="recebiveis",
            sharding=ShardingConfig(
                discriminator_column="cnpj",
                resolver="examples.shard_resolver_example:resolve_cnpj_shard",
            ),
            columns=[ColumnConfig(name="cnpj"), ColumnConfig(name="valor")],
        )
    ],
)
reg3 = DatabaseRegistry(cfg3)
plan_one = IntentPlan(
    filters=[
        FilterClause(
            table_id="recebiveis",
            column_id="cnpj",
            op="eq",
            value="12.345.678/0001-90",
        )
    ],
    metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
)
routing = resolve_routing(plan_one, cfg3, registry=reg3)
check("resolve_routing mode single", routing.mode == "single")
check("resolve CNPJ prefixo 123 -> db_shard_1", routing.bindings[0].database_id == "db_shard_1")
check(
    "resolve nome físico recebiveis_123",
    routing.bindings[0].physical_table == "recebiveis_123",
)
plan_two = IntentPlan(
    filters=[
        FilterClause(
            table_id="recebiveis",
            column_id="cnpj",
            op="eq",
            value="40000000000100",
        )
    ],
)
routing2 = resolve_routing(plan_two, cfg3, registry=reg3)
check("resolve CNPJ prefixo 400 -> db_shard_2", routing2.bindings[0].database_id == "db_shard_2")
plan_missing = IntentPlan(
    metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
)
out_clarify = resolve_routing(plan_missing, cfg3, registry=reg3)
check(
    "discriminador ausente pede clarificação (sem fan-out)",
    isinstance(out_clarify, ClarifyNeeded),
)


# --------------------------------------------------------------------------- #
# 5. Camada DuckDB
# --------------------------------------------------------------------------- #
print("\n== DuckDB layer ==")
duck_table = TableConfig(
    id="recebiveis",
    database="db_shard_1",
    name="recebiveis_001",
    duckdb=DuckDBConfig(enabled=True, trigger="aggregation", fetch_limit=1000),
)

session = DuckDBSession()
session.materialize(duck_table, reg3.get_engine("db_shard_1"), physical_name="recebiveis_001")
result = session.execute("SELECT SUM(valor) AS total FROM recebiveis")
check("DuckDB materializa e agrega", result and result[0]["total"] == 10.0)
session.close()


# --------------------------------------------------------------------------- #
# 6. load_config a partir do YAML de exemplo
# --------------------------------------------------------------------------- #
print("\n== load_config (recebiveis.yaml) ==")
import os

os.environ.update(
    MAIN_DB_URL=f"sqlite:///{main_db}",
    SHARD_1_DB_URL=f"sqlite:///{shard_dbs[1]}",
    SHARD_2_DB_URL=f"sqlite:///{shard_dbs[2]}",
    SHARD_3_DB_URL=f"sqlite:///{shard_dbs[3]}",
)
cfg_yaml = load_config("examples/recebiveis.yaml")
check("YAML: 4 bancos", len(cfg_yaml.databases) == 4)
check("YAML: recebiveis é shardada", cfg_yaml.get_table("recebiveis").is_sharded)
check("YAML: recebiveis usa duckdb", cfg_yaml.get_table("recebiveis").uses_duckdb)
check("YAML: sample_rows=50", cfg_yaml.sample_rows == 50)

# prompt builder
prompt = Txt2SqlPromptBuilder(cfg_yaml).build()
check("prompt tem seção de sharding", "SHARDADA" in prompt or "Sharding" in prompt)
check("prompt proíbe fan-out", "fan-out" in prompt.lower())
check("prompt tem descrições de coluna", "CNPJ do titular" in prompt)

# diario.yaml
os.environ["MSSQL_URL"] = "sqlite:///:memory:"
cfg_diario = load_config("examples/diario.yaml")
check("diario.yaml carrega (3 tabelas)", len(cfg_diario.tables) == 3)
check("diario.yaml dialeto tsql", cfg_diario.dialect == "tsql")


print(f"\n=== RESULTADO: {'TODOS OS TESTES PASSARAM' if FAILS == 0 else str(FAILS) + ' FALHA(S)'} ===")
raise SystemExit(1 if FAILS else 0)
