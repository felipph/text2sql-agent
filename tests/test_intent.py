"""Validação programática do IntentPlan."""

from __future__ import annotations

from txt2sql.intent import (
    Clarification,
    EntityRef,
    FilterClause,
    IntentPlan,
    JoinClause,
    JoinOn,
    MetricClause,
    validate_intent,
)

INDEX = {
    "clientes": {"cnpj", "razao_social"},
    "recebiveis": {"cnpj", "valor", "status"},
}


def test_ready_valid_plan() -> None:
    plan = IntentPlan(
        status="ready",
        question_rewrite="Soma dos recebíveis do CNPJ X",
        entities=[EntityRef(mention="recebíveis", table_id="recebiveis", role="table")],
        filters=[
            FilterClause(table_id="recebiveis", column_id="cnpj", op="eq", value="X"),
        ],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    result = validate_intent(plan, INDEX)
    assert result.ok
    assert result.errors == []


def test_unknown_table_fails() -> None:
    plan = IntentPlan(
        status="ready",
        question_rewrite="x",
        metrics=[MetricClause(table_id="fantasma", column_id=None, agg="count")],
    )
    result = validate_intent(plan, INDEX)
    assert not result.ok
    assert any("fantasma" in e for e in result.errors)


def test_unknown_column_fails() -> None:
    plan = IntentPlan(
        status="ready",
        question_rewrite="x",
        filters=[FilterClause(table_id="clientes", column_id="foo", op="eq", value="1")],
    )
    result = validate_intent(plan, INDEX)
    assert not result.ok


def test_bad_join_fails() -> None:
    plan = IntentPlan(
        status="ready",
        question_rewrite="x",
        joins=[
            JoinClause(
                from_table_id="clientes",
                to_table_id="recebiveis",
                on=[JoinOn(from_column="nope", to_column="cnpj")],
            )
        ],
    )
    result = validate_intent(plan, INDEX)
    assert not result.ok


def test_needs_clarification_skips_schema_checks() -> None:
    plan = IntentPlan(
        status="needs_clarification",
        question_rewrite="x",
        clarification=Clarification(question="Qual período?"),
        metrics=[MetricClause(table_id="fantasma", agg="count")],
    )
    result = validate_intent(plan, INDEX)
    assert result.ok
    assert result.needs_clarification


def test_get_column_index_declarative() -> None:
    from txt2sql.config import AgentConfig, ColumnConfig, DatabaseConfig, TableConfig
    from txt2sql.db.registry import DatabaseRegistry
    from txt2sql.db.schema import SchemaLoader

    config = AgentConfig(
        databases=[DatabaseConfig(id="db", connection_string="sqlite:///:memory:")],
        tables=[
            TableConfig(
                id="clientes",
                database="db",
                name="clientes",
                columns=[ColumnConfig(name="cnpj"), ColumnConfig(name="razao_social")],
            )
        ],
    )
    loader = SchemaLoader(config, DatabaseRegistry(config))
    index = loader.get_column_index()
    assert index == {"clientes": {"cnpj", "razao_social"}}


def test_get_column_index_discovery() -> None:
    import sqlite3
    import tempfile
    from pathlib import Path

    from txt2sql.config import AgentConfig, DatabaseConfig, TableConfig
    from txt2sql.db.registry import DatabaseRegistry
    from txt2sql.db.schema import SchemaLoader

    tmp = tempfile.mkdtemp()
    db = Path(tmp) / "t.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE clientes (cnpj TEXT, razao_social TEXT)")
    c.commit()
    c.close()

    config = AgentConfig(
        databases=[DatabaseConfig(id="db", connection_string=f"sqlite:///{db}")],
        tables=[TableConfig(id="clientes", database="db", name="clientes")],
    )
    loader = SchemaLoader(config, DatabaseRegistry(config))
    index = loader.get_column_index()
    assert index["clientes"] == {"cnpj", "razao_social"}


def test_intent_plan_json_schema_has_typed_filter_value() -> None:
    """Azure/OpenAI rejeita properties sem `type` (ex.: Any em FilterClause.value)."""
    schema = IntentPlan.model_json_schema()
    # Pydantic $defs (v2) ou definitions
    defs = schema.get("$defs") or schema.get("definitions") or {}
    filter_schema = defs.get("FilterClause") or schema
    props = filter_schema.get("properties") or {}
    value_schema = props.get("value")
    assert value_schema is not None
    assert "type" in value_schema or "anyOf" in value_schema


def test_build_intent_prompt_mentions_clarification() -> None:
    from txt2sql.config import AgentConfig, DatabaseConfig, GlossaryEntry, TableConfig
    from txt2sql.prompts import Txt2SqlPromptBuilder

    config = AgentConfig(
        databases=[DatabaseConfig(id="db", connection_string="sqlite:///:memory:")],
        tables=[TableConfig(id="clientes", database="db", name="clientes", description="Cadastro")],
        glossary=[GlossaryEntry(term="CNPJ", definition="identificador")],
    )
    text = Txt2SqlPromptBuilder(config).build_intent_prompt()
    assert "IntentPlan" in text
    assert "needs_clarification" in text
    assert "CNPJ" in text
    assert "Cadastro" in text


def test_build_intent_prompt_requires_discriminator_in_filters() -> None:
    from txt2sql.config import (
        AgentConfig,
        DatabaseConfig,
        ShardingConfig,
        TableConfig,
    )
    from txt2sql.prompts import Txt2SqlPromptBuilder

    config = AgentConfig(
        databases=[DatabaseConfig(id="db", connection_string="sqlite:///:memory:")],
        tables=[
            TableConfig(
                id="recebiveis",
                database="db",
                name="recebiveis",
                sharding=ShardingConfig(
                    discriminator_column="tenant_id",
                    resolver="playground.shard_resolver:resolve_cnpj_shard",
                ),
            )
        ],
    )
    text = Txt2SqlPromptBuilder(config).build_intent_prompt()
    assert "discriminador" in text.lower()
    assert "filters" in text
    assert "question_rewrite" in text
    assert "tenant_id" in text
    assert "CNPJ" not in text  # prompt parametrizado — sem domínio hardcoded
    assert "adicione" in text.lower() or "acumul" in text.lower()
    assert "união" in text.lower() or "uniao" in text.lower() or "op=in" in text
