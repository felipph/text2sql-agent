"""Exemplo de resolver + value_extractor de shard (domínio: CNPJ).

Adapters referenciados no YAML — o core txt2sql não conhece CNPJ:

    sharding:
      discriminator_column: cnpj
      resolver: "examples.shard_resolver_example:resolve_cnpj_shard"
      value_extractor: "examples.shard_resolver_example:extract_cnpj_values"
"""

from __future__ import annotations

import re

from txt2sql.config import ShardResult

_CNPJ_RE = re.compile(r"\b(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}|\d{14})\b")


def _normalize_cnpj(cnpj: str) -> str:
    """Remove formatação do CNPJ, deixando apenas dígitos."""
    digits = "".join(ch for ch in cnpj if ch.isdigit())
    if len(digits) != 14:
        raise ValueError(
            f"CNPJ inválido: esperados 14 dígitos, obtidos {len(digits)} ({cnpj!r})."
        )
    return digits


def resolve_cnpj_shard(cnpj: str) -> ShardResult:
    """Resolve o shard físico de um recebível a partir do CNPJ.

    Estratégia determinística:
        * ``prefix`` = 3 primeiros dígitos do CNPJ (000–999).
        * Nome físico da tabela: ``recebiveis_<prefix>``.
        * Roteamento de banco por faixa do prefixo:
            - 000–333 -> ``db_shard_1``
            - 334–666 -> ``db_shard_2``
            - 667–999 -> ``db_shard_3``
    """
    digits = _normalize_cnpj(cnpj)
    prefix = digits[:3]
    prefix_int = int(prefix)

    if prefix_int <= 333:
        database_id = "db_shard_1"
    elif prefix_int <= 666:
        database_id = "db_shard_2"
    else:
        database_id = "db_shard_3"

    return ShardResult(database_id=database_id, table_name=f"recebiveis_{prefix}")


def extract_cnpj_values(text: str) -> list[str]:
    """Extrai CNPJs (14 dígitos) de texto livre — ``value_extractor`` do YAML."""
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


if __name__ == "__main__":  # demonstração rápida
    for exemplo in ("12.345.678/0001-90", "400.000.000/0001-00", "99900000000000"):
        try:
            print(f"{exemplo!r:30} -> {resolve_cnpj_shard(exemplo)}")
        except ValueError as err:
            print(f"{exemplo!r:30} -> ERRO: {err}")
    print("extract:", extract_cnpj_values("CNPJs 65410433218196 e 74778161849593"))
