"""Testes de formato da resposta (sem proveniência; partial em linguagem natural)."""

from __future__ import annotations

from txt2sql.answer_grounding import (
    build_partial_user_notice,
    format_answer_from_sample,
    strip_provenance_from_answer,
)
from txt2sql.artifacts import ExecutionResult


def test_strip_provenance_from_answer() -> None:
    text = (
        "Segue a tabela.\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "---\n"
        "**Proveniência**\n"
        "- **SQL executado:** `SELECT 1`\n"
        "- **Parcial:** sim\n"
    )
    cleaned = strip_provenance_from_answer(text)
    assert "Proveniência" not in cleaned
    assert "SQL executado" not in cleaned
    assert "Segue a tabela" in cleaned
    assert "| 1 | 2 |" in cleaned


def test_strip_provenance_plain_footer() -> None:
    text = "Ok.\n\n---\nSQL: SELECT 1\nParcial: não\nStatus: ok"
    assert "SQL:" not in strip_provenance_from_answer(text)
    assert strip_provenance_from_answer(text).strip() == "Ok."


def test_build_partial_user_notice_from_cap_assumption() -> None:
    notice = build_partial_user_notice(
        assumptions=["Cobertura parcial: 20 de 625 shards físicos (max_shards=20)"],
        max_shards=20,
    )
    assert notice is not None
    low = notice.lower()
    assert "incompleta" in low
    assert "20" in notice
    assert "625" in notice
    assert "max_shards" not in low
    assert "cnpj" in low or "período" in low or "top" in low


def test_build_partial_user_notice_none_when_complete() -> None:
    assert build_partial_user_notice(assumptions=[], partial=False) is None


def test_format_answer_partial_is_natural() -> None:
    last = ExecutionResult(
        status="ok",
        row_count=1,
        sample=[{"cnpj": "111", "pct": 0.5}],
    )
    text = format_answer_from_sample(
        last,
        partial=True,
        assumptions=["Cobertura parcial: 20 de 625 shards físicos (max_shards=20)"],
        max_shards=20,
    )
    assert "111" in text
    assert "max_shards" not in text.lower()
    assert "incompleta" in text.lower()
    assert "Proveniência" not in text
    assert "Assunções:" not in text
