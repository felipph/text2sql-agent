"""Resolver e extractors de shard do playground (domínio: CNPJ).

O core txt2sql não conhece CNPJ — estes callables são adapters referenciados
no YAML via ``sharding.resolver`` / ``sharding.value_extractor``.
"""

from __future__ import annotations

import re

from txt2sql.config import ShardResult

_CNPJ_RE = re.compile(r"\b(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}|\d{14})\b")


def _normalize_cnpj(cnpj: str) -> str:
    """Remove formatação e valida 14 dígitos."""
    digits = "".join(ch for ch in cnpj if ch.isdigit())
    if len(digits) != 14:
        raise ValueError(
            f"CNPJ inválido: esperados 14 dígitos, obtidos {len(digits)} ({cnpj!r})."
        )
    return digits


def resolve_cnpj_shard(cnpj: str) -> ShardResult:
    """Resolve shard físico: 000–499 → db_shard_1; 500–999 → db_shard_2."""
    digits = _normalize_cnpj(cnpj)
    prefix = digits[:3]
    database_id = "db_shard_1" if int(prefix) <= 499 else "db_shard_2"
    return ShardResult(database_id=database_id, table_name=f"recebiveis_{prefix}")


def extract_cnpj_values(text: str) -> list[str]:
    """Extrai CNPJs (14 dígitos) de texto livre — ``value_extractor`` do YAML.

    Usado como fallback quando o IntentPlan omite o discriminador em filters.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _CNPJ_RE.finditer(text):
        digits = "".join(ch for ch in match.group(1) if ch.isdigit())
        if len(digits) != 14 or digits in seen:
            continue
        seen.add(digits)
        out.append(digits)
    return out
