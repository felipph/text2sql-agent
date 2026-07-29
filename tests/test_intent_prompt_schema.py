"""Intent prompt inclui colunas discovery (nome + tipo) sem columns no YAML."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from txt2sql.config import (
    AgentConfig,
    ColumnConfig,
    DatabaseConfig,
    TableConfig,
)
from txt2sql.db.registry import DatabaseRegistry
from txt2sql.db.schema import SchemaLoader
from txt2sql.prompts import Txt2SqlPromptBuilder


def _cfg_with_real_clientes() -> tuple[AgentConfig, Path]:
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "main.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE clientes ("
        "  cnpj TEXT NOT NULL,"
        "  razao_social TEXT"
        ");"
    )
    conn.commit()
    conn.close()
    cfg = AgentConfig(
        databases=[
            DatabaseConfig(id="db_main", connection_string=f"sqlite:///{db_path}")
        ],
        tables=[
            TableConfig(
                id="clientes",
                database="db_main",
                name="clientes",
                description="Cadastro de clientes.",
            ),
            TableConfig(
                id="recebiveis",
                database="db_main",
                name="recebiveis",
                description="Recebíveis.",
                columns=[
                    ColumnConfig(
                        name="valor", type="NUMERIC", description="Valor bruto BRL."
                    )
                ],
            ),
        ],
    )
    return cfg, db_path


def test_schema_loader_list_columns_discovery() -> None:
    cfg, _ = _cfg_with_real_clientes()
    loader = SchemaLoader(cfg, DatabaseRegistry(cfg))
    cols = loader.list_columns("clientes")
    by_name = {c["name"]: c["type"] for c in cols}
    assert "cnpj" in by_name
    assert "razao_social" in by_name
    assert by_name["cnpj"]  # tipo não vazio


def test_schema_loader_list_columns_declarative() -> None:
    cfg, _ = _cfg_with_real_clientes()
    loader = SchemaLoader(cfg, DatabaseRegistry(cfg))
    cols = loader.list_columns("recebiveis")
    assert cols == [
        {"name": "valor", "type": "NUMERIC", "description": "Valor bruto BRL."}
    ]


def test_intent_prompt_includes_discovered_columns() -> None:
    cfg, _ = _cfg_with_real_clientes()
    loader = SchemaLoader(cfg, DatabaseRegistry(cfg))
    text = Txt2SqlPromptBuilder(cfg).build_intent_prompt(schema_loader=loader)
    assert "## 9. Semântica das colunas" in text
    assert "### Tabela `clientes`" in text
    assert "`cnpj`" in text
    assert "`razao_social`" in text
    assert "### Tabela `recebiveis`" in text
    assert "`valor` (NUMERIC)" in text
    assert "Valor bruto BRL." in text
