"""Testes do SELECT denormalizado para export."""

from __future__ import annotations

from txt2sql.config import (
    AgentConfig,
    ColumnConfig,
    ColumnRef,
    DatabaseConfig,
    RelationshipConfig,
    TableConfig,
)
from txt2sql.export_csv import build_denormalized_select, detect_wants_export
from txt2sql.intent import (
    EntityRef,
    FilterClause,
    IntentPlan,
    JoinClause,
    JoinOn,
    MetricClause,
)


def _cfg() -> AgentConfig:
    return AgentConfig(
        databases=[DatabaseConfig(id="db", connection_string="sqlite:///:memory:")],
        tables=[
            TableConfig(
                id="clientes",
                database="db",
                name="clientes",
                columns=[ColumnConfig(name="cnpj"), ColumnConfig(name="razao_social")],
            ),
            TableConfig(
                id="recebiveis",
                database="db",
                name="recebiveis",
                columns=[
                    ColumnConfig(name="cnpj"),
                    ColumnConfig(name="valor"),
                    ColumnConfig(name="status"),
                ],
            ),
        ],
        relationships=[
            RelationshipConfig(
                from_ref=ColumnRef(table="recebiveis", column="cnpj"),
                to_ref=ColumnRef(table="clientes", column="cnpj"),
            )
        ],
    )


def test_detect_wants_export() -> None:
    assert detect_wants_export("exporte a lista em CSV")
    assert detect_wants_export("quero baixar a planilha")
    assert not detect_wants_export("qual o total por cliente?")


def test_build_denormalized_select_join() -> None:
    plan = IntentPlan(
        wants_export=True,
        entities=[
            EntityRef(mention="c", table_id="clientes", role="table"),
            EntityRef(mention="r", table_id="recebiveis", role="table"),
        ],
        joins=[
            JoinClause(
                from_table_id="recebiveis",
                to_table_id="clientes",
                on=[JoinOn(from_column="cnpj", to_column="cnpj")],
            )
        ],
        metrics=[MetricClause(table_id="recebiveis", column_id="valor", agg="sum")],
    )
    sql = build_denormalized_select(plan, _cfg())
    upper = sql.upper()
    assert "SUM(" not in upper
    assert "GROUP BY" not in upper
    assert "JOIN" in upper
    assert '"recebiveis"' in sql
    assert '"clientes"' in sql


def test_build_denormalized_select_single_table() -> None:
    plan = IntentPlan(
        wants_export=True,
        entities=[EntityRef(mention="r", table_id="recebiveis", role="table")],
        filters=[FilterClause(table_id="recebiveis", column_id="status", op="eq", value="pago")],
    )
    sql = build_denormalized_select(plan, _cfg())
    assert "JOIN" not in sql.upper()
    assert "pago" in sql
