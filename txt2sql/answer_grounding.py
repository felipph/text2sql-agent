"""Grounding da resposta final — evita narrar falha quando há resultado ok."""

from __future__ import annotations

import re
from typing import Any

from txt2sql.artifacts import AnswerProvenance, ExecutionResult, ShardRouting

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

_CAP_ASSUMPTION_RE = re.compile(
    r"Cobertura parcial:\s*(\d+)\s*de\s*(\d+)\s*shards",
    re.IGNORECASE,
)
_LOOKUP_TRUNC_RE = re.compile(r"truncada no lookup", re.IGNORECASE)
_PROVENANCE_SPLIT_RE = re.compile(
    r"\n\s*---\s*\n(?:\s*\*?\*?Proveni[eê]ncia\*?\*?.*)?",
    re.IGNORECASE | re.DOTALL,
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


def strip_provenance_from_answer(text: str) -> str:
    """Remove bloco de proveniência (após '---') da resposta voltada ao usuário."""
    if not text:
        return text
    # Corta no primeiro separador de proveniência típico
    parts = _PROVENANCE_SPLIT_RE.split(text, maxsplit=1)
    cleaned = parts[0].rstrip()
    # Também remove rodapé sem título se vier só com SQL:/Parcial:/Status:
    lines = cleaned.splitlines()
    while lines:
        last = lines[-1].strip().lower()
        if last.startswith(("sql:", "sql ", "assun", "parcial:", "status:", "avisos:")):
            lines.pop()
            continue
        if last == "---":
            lines.pop()
            continue
        break
    return "\n".join(lines).rstrip()


def build_partial_user_notice(
    *,
    assumptions: list[str] | None = None,
    partial: bool = True,
    max_shards: int | None = None,
    shard_routing: ShardRouting | None = None,
) -> str | None:
    """Aviso em linguagem natural quando a resposta é incompleta.

    Sem nomes de parâmetros técnicos (max_shards, FilterClause, etc.).
    """
    assumptions = list(assumptions or [])
    has_cap = any(_CAP_ASSUMPTION_RE.search(a) for a in assumptions)
    has_lookup_trunc = any(_LOOKUP_TRUNC_RE.search(a) for a in assumptions)
    is_partial = bool(partial) or bool(shard_routing and shard_routing.capped) or has_cap or has_lookup_trunc
    if not is_partial:
        return None

    kept: int | None = None
    total: int | None = None
    for a in assumptions:
        m = _CAP_ASSUMPTION_RE.search(a)
        if m:
            kept = int(m.group(1))
            total = int(m.group(2))
            break

    if kept is None and shard_routing is not None and shard_routing.capped:
        phys = {(b.database_id, b.physical_table) for b in shard_routing.bindings}
        kept = len(phys)

    limit = max_shards if max_shards is not None else kept

    if kept is not None and total is not None and total > kept:
        lim = limit if limit is not None else kept
        reason = (
            f"A informação está incompleta: neste turno só consigo analisar até "
            f"{lim} grupos de dados particionados, e sua pergunta cobria {total} "
            f"(usei {kept} deles)."
        )
    elif has_lookup_trunc:
        reason = (
            "A informação está incompleta: a lista de identificadores obtida "
            "para a consulta foi limitada neste turno."
        )
    else:
        lim = limit if limit is not None else "um número limitado de"
        reason = (
            f"A informação está incompleta: neste turno só consigo analisar "
            f"até {lim} grupos de dados particionados."
        )

    suggestion = (
        "Para obter o máximo possível com essa limitação, refine a pergunta: "
        "delimite um conjunto menor de clientes/CNPJs, um período específico, "
        "ou peça um ranking top-N com recorte mais estreito."
    )
    return f"{reason} {suggestion}"


def build_answer_provenance(
    *,
    sql_history: list[str],
    assumptions: list[str],
    partial: bool,
    last_result: ExecutionResult | None,
) -> AnswerProvenance:
    """Proveniência para o state/trace — não vai na mensagem ao usuário."""
    status = last_result.status if last_result is not None else None
    warnings = list(last_result.warnings or []) if last_result is not None else []
    row_count = last_result.row_count if last_result is not None else None
    return AnswerProvenance(
        sql_history=list(sql_history),
        assumptions=list(assumptions),
        partial=partial,
        last_result_status=status,
        last_result_warnings=warnings,
        row_count=row_count,
    )


def format_answer_from_sample(
    last: ExecutionResult,
    *,
    partial: bool = False,
    assumptions: list[str] | None = None,
    max_shards: int | None = None,
    shard_routing: ShardRouting | None = None,
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
    notice = build_partial_user_notice(
        assumptions=assumptions,
        partial=partial,
        max_shards=max_shards,
        shard_routing=shard_routing,
    )
    if notice:
        lines.extend(["", notice])
    return "\n".join(lines)


def _fmt_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value).replace("|", "\\|")


__all__ = [
    "answer_contradicts_ok_result",
    "build_answer_provenance",
    "build_partial_user_notice",
    "filter_messages_for_answer",
    "format_answer_from_sample",
    "is_discriminator_retry_content",
    "strip_provenance_from_answer",
]
