"""Testes dos adapters de domínio do playground (CNPJ)."""

from __future__ import annotations

from playground.shard_resolver import extract_cnpj_values, resolve_cnpj_shard


def test_extract_cnpj_values_from_text() -> None:
    text = (
        "Qual a soma dos CNPJs 65410433218196 e 74778161849593? "
        "Monte uma tabela."
    )
    assert extract_cnpj_values(text) == ["65410433218196", "74778161849593"]


def test_extract_cnpj_values_formatted() -> None:
    assert extract_cnpj_values("CNPJ 12.345.678/0001-90") == ["12345678000190"]


def test_resolve_cnpj_shard_prefix() -> None:
    r = resolve_cnpj_shard("65410433218196")
    assert r.database_id == "db_shard_2"
    assert r.table_name == "recebiveis_654"
