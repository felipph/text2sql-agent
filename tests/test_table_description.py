"""Testes de description negocial em tabelas."""

from __future__ import annotations

from pathlib import Path

import yaml

from txt2sql.config import (
    AgentConfig,
    ColumnConfig,
    DatabaseConfig,
    TableConfig,
    load_config,
)
from txt2sql.db.schema import SchemaLoader
from txt2sql.prompts import Txt2SqlPromptBuilder


def _write_yaml(tmp_path: Path, tables: list[dict]) -> Path:
    data = {
        "dialect": "postgres",
        "databases": [
            {"id": "db_main", "connection_string": "sqlite:///:memory:", "read_only": True}
        ],
        "tables": tables,
    }
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


def test_load_config_preserves_table_description(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        [
            {
                "id": "clientes",
                "database": "db_main",
                "name": "clientes",
                "description": "Cadastro de clientes.",
            },
            {
                "id": "pedidos",
                "database": "db_main",
                "name": "pedidos",
            },
        ],
    )
    config = load_config(path)
    assert config.get_table("clientes").description == "Cadastro de clientes."
    assert config.get_table("pedidos").description is None


def test_schema_loader_includes_table_description() -> None:
    table = TableConfig(
        id="recebiveis",
        database="db_main",
        name="recebiveis",
        schema="public",
        description="Títulos a receber shardados por CNPJ.",
        columns=[ColumnConfig(name="cnpj", type="VARCHAR", description="CNPJ")],
    )
    config = AgentConfig(
        databases=[DatabaseConfig(id="db_main", connection_string="sqlite:///:memory:")],
        tables=[table],
    )
    # registry não é usado no caminho declarativo
    loader = SchemaLoader(config, registry=None)  # type: ignore[arg-type]
    info = loader.get_table_info("recebiveis")
    assert "Descrição: Títulos a receber shardados por CNPJ." in info
    assert "cnpj" in info


def test_schema_loader_omits_description_when_absent() -> None:
    table = TableConfig(
        id="clientes",
        database="db_main",
        name="clientes",
        columns=[ColumnConfig(name="id", type="INT")],
    )
    config = AgentConfig(
        databases=[DatabaseConfig(id="db_main", connection_string="sqlite:///:memory:")],
        tables=[table],
    )
    loader = SchemaLoader(config, registry=None)  # type: ignore[arg-type]
    info = loader.get_table_info("clientes")
    assert "Descrição:" not in info


def test_prompt_includes_table_semantics_section() -> None:
    config = AgentConfig(
        databases=[DatabaseConfig(id="db_main", connection_string="sqlite:///:memory:")],
        tables=[
            TableConfig(
                id="clientes",
                database="db_main",
                name="clientes",
                description="Cadastro de clientes.",
            ),
            TableConfig(
                id="pedidos",
                database="db_main",
                name="pedidos",
            ),
        ],
    )
    prompt = Txt2SqlPromptBuilder(config).build()
    assert "## 8. Semântica das tabelas" in prompt
    assert "`clientes`: Cadastro de clientes." in prompt
    assert "`pedidos`" not in prompt.split("## 8. Semântica das tabelas")[1].split("##")[0]
    assert "## 9. Semântica das colunas" not in prompt  # sem columns declaradas nem loader


def test_prompt_omits_table_section_when_no_descriptions() -> None:
    config = AgentConfig(
        databases=[DatabaseConfig(id="db_main", connection_string="sqlite:///:memory:")],
        tables=[
            TableConfig(id="clientes", database="db_main", name="clientes"),
        ],
    )
    prompt = Txt2SqlPromptBuilder(config).build()
    assert "Semântica das tabelas" not in prompt


def test_prompt_renumbers_column_section_after_tables() -> None:
    config = AgentConfig(
        databases=[DatabaseConfig(id="db_main", connection_string="sqlite:///:memory:")],
        tables=[
            TableConfig(
                id="recebiveis",
                database="db_main",
                name="recebiveis",
                description="Recebíveis.",
                columns=[ColumnConfig(name="valor", type="NUMERIC")],
            ),
        ],
    )
    prompt = Txt2SqlPromptBuilder(config).build()
    assert "## 8. Semântica das tabelas" in prompt
    assert "## 9. Semântica das colunas" in prompt
    assert "## 10. Tabelas volumétricas" not in prompt  # sem duckdb
    # custom seria 11 se existisse; volumétricas/custom só se aplicáveis
    assert "### Tabela `recebiveis`" in prompt
