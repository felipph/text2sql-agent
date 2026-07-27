"""Testes do extrator de debug do playground."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from playground.debug_view import extract_turn_debug


def test_extract_resolve_and_query() -> None:
    messages = [
        HumanMessage(content="soma?"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "resolve_shard",
                    "args": {
                        "table_id": "recebiveis",
                        "discriminator_value": "12345678000190",
                    },
                    "id": "1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content='{"database_id":"db_shard_1","table_name":"recebiveis_123"}',
            tool_call_id="1",
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "sql_db_query",
                    "args": {"query": "SELECT SUM(valor) FROM recebiveis_123"},
                    "id": "2",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="[{'sum': 175}]", tool_call_id="2"),
        AIMessage(content="A soma é 175."),
    ]
    debug = extract_turn_debug(messages)
    assert len(debug.steps) == 2
    assert debug.steps[0].name == "resolve_shard"
    assert "db_shard_1" in debug.steps[0].result
    assert debug.steps[1].name == "sql_db_query"
    assert "SUM" in (debug.steps[1].args.get("query") or "")
    assert debug.final_answer == "A soma é 175."
    assert debug.looks_like_guardrail_reject is False


def test_guardrail_reject_flag() -> None:
    messages = [
        HumanMessage(content="delete"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "sql_db_query",
                    "args": {"query": "DELETE FROM x"},
                    "id": "1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content="Erro de guardrail: apenas SELECT permitido",
            tool_call_id="1",
        ),
        AIMessage(content="Não posso apagar."),
    ]
    debug = extract_turn_debug(messages)
    assert debug.looks_like_guardrail_reject is True
