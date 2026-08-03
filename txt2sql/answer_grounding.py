"""Grounding da resposta final — evita narrar falha quando há resultado ok."""

from __future__ import annotations

from typing import Any

from txt2sql.artifacts import ExecutionResult

_DISC_RETRY_MARKERS = (
    "falta o discriminador de shard",
    "exige filterclause em filters",
    "não deixe o valor só em question_rewrite",
)

_FAILURE_MARKERS = (
    "não consegui",
    "nao consegui",
    "não foi possível executar",
    "nao foi possivel executar",
    "faltou aplicar",
    "consulta ainda porque faltou",
    "sql executado:** nenhum",
    "sql executado: nenhum",
)


def is_discriminator_retry_content(content: str) -> bool:
    low = (content or "").lower()
    return any(m in low for m in _DISC_RETRY_MARKERS)


def filter_messages_for_answer(messages: list[Any]) -> list[Any]:
    """Remove SystemMessages de retry de discriminador do histórico enviado ao answer."""
    out: list[Any] = []
    for msg in messages:
        typ = getattr(msg, "type", None)
        content = getattr(msg, "content", "") or ""
        if typ == "system" and is_discriminator_retry_content(str(content)):
            continue
        out.append(msg)
    return out


def answer_contradicts_ok_result(text: str, last: ExecutionResult | None) -> bool:
    """True se o texto narra falha mas last_result está ok com sample."""
    if last is None or last.status != "ok" or not last.sample:
        return False
    low = (text or "").lower()
    return any(m in low for m in _FAILURE_MARKERS)


def format_answer_from_sample(
    last: ExecutionResult,
    *,
    partial: bool = False,
    assumptions: list[str] | None = None,
) -> str:
    """Monta resposta determinística (tabela markdown) a partir do sample."""
    rows = list(last.sample or [])
    if not rows:
        return "Consulta executada com sucesso, mas sem linhas no resultado."

    cols = list(rows[0].keys())
    header = "| " + " | ".join(str(c) for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body_lines: list[str] = []
    for row in rows:
        body_lines.append(
            "| " + " | ".join(_fmt_cell(row.get(c)) for c in cols) + " |"
        )

    lines = [
        "Resultado da consulta:",
        "",
        header,
        sep,
        *body_lines,
    ]
    if partial:
        lines.extend(["", "_Resultado parcial (cobertura de shards limitada)._"])
    if assumptions:
        relevant = [a for a in assumptions if a.strip()]
        if relevant:
            lines.extend(["", "Observações: " + "; ".join(relevant)])
    return "\n".join(lines)


def _fmt_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value).replace("|", "\\|")


__all__ = [
    "answer_contradicts_ok_result",
    "filter_messages_for_answer",
    "format_answer_from_sample",
    "is_discriminator_retry_content",
]
