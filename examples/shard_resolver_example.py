"""Exemplo de resolver de shard determinístico por CNPJ.

Um resolver é qualquer callable ``(discriminator_value: str) -> ShardResult``.
Ele é referenciado no YAML via caminho dotted, por exemplo:

    sharding:
      discriminator_column: cnpj
      resolver: "examples.shard_resolver_example:resolve_cnpj_shard"

A resolução deve ser **determinística**: o mesmo CNPJ sempre mapeia para o mesmo
banco físico e o mesmo nome de tabela. Aqui usamos os 3 primeiros dígitos do
CNPJ como sufixo da tabela (``recebiveis_000`` .. ``recebiveis_999``) e dividimos
o espaço 000–999 em três shards físicos.
"""

from __future__ import annotations

from txt2sql.config import ShardResult


def _normalize_cnpj(cnpj: str) -> str:
    """Remove formatação do CNPJ, deixando apenas dígitos.

    Args:
        cnpj: CNPJ possivelmente formatado (ex.: ``12.345.678/0001-90``).

    Returns:
        String contendo apenas os dígitos do CNPJ.

    Raises:
        ValueError: Se o CNPJ não contiver 14 dígitos.
    """
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

    Args:
        cnpj: CNPJ do titular (formatado ou apenas dígitos).

    Returns:
        Um :class:`ShardResult` com o ``database_id`` e o ``table_name`` físicos.
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


if __name__ == "__main__":  # demonstração rápida
    for exemplo in ("12.345.678/0001-90", "400.000.000/0001-00", "99900000000000"):
        try:
            print(f"{exemplo!r:30} -> {resolve_cnpj_shard(exemplo)}")
        except ValueError as err:
            print(f"{exemplo!r:30} -> ERRO: {err}")
