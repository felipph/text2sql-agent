"""Resolver de shard do playground: 2 faixas por prefixo de CNPJ."""

from __future__ import annotations

from txt2sql.config import ShardResult


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
