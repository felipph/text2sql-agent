"""Testes de grounding da resposta final."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from txt2sql.answer_grounding import (
    answer_contradicts_ok_result,
    filter_messages_for_answer,
    format_answer_from_sample,
    is_discriminator_retry_content,
)
from txt2sql.artifacts import ExecutionResult


def test_is_discriminator_retry_content() -> None:
    assert is_discriminator_retry_content(
        "O IntentPlan está ready mas falta o discriminador de shard em filters."
    )
    assert not is_discriminator_retry_content("Schema inválido: coluna x")


def test_filter_messages_for_answer_drops_disc_retry() -> None:
    msgs = [
        HumanMessage(content="top 10"),
        SystemMessage(
            content=(
                "O IntentPlan está ready mas falta o discriminador de shard "
                "em filters. Corrija e tente de novo."
            )
        ),
        AIMessage(content="ok"),
    ]
    filtered = filter_messages_for_answer(msgs)
    assert len(filtered) == 2
    assert all(getattr(m, "type", None) != "system" for m in filtered)


def test_answer_contradicts_ok_result() -> None:
    last = ExecutionResult(
        status="ok",
        row_count=2,
        sample=[{"cnpj": "1", "pct": 0.5}, {"cnpj": "2", "pct": 0.6}],
    )
    assert answer_contradicts_ok_result(
        "Não consegui fechar a consulta porque falta filtro de cnpj.", last
    )
    assert not answer_contradicts_ok_result(
        "Segue o top 2:\n| cnpj | pct |\n|---|---|\n| 1 | 0.5 |", last
    )
    assert not answer_contradicts_ok_result(
        "Não consegui", ExecutionResult(status="error", error="x")
    )


def test_format_answer_from_sample() -> None:
    last = ExecutionResult(
        status="ok",
        row_count=2,
        sample=[
            {"cnpj": "111", "razao_social": "A", "pct": 0.52},
            {"cnpj": "222", "razao_social": "B", "pct": 0.61},
        ],
    )
    text = format_answer_from_sample(
        last,
        partial=True,
        assumptions=["Cobertura parcial: 20 de 625 shards"],
    )
    assert "111" in text
    assert "razao_social" in text
    assert "parcial" in text.lower()
    assert "625" in text
