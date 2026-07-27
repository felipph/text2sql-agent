"""Testes do resolver de shard do playground (2 faixas)."""

from __future__ import annotations

import pytest

from playground.shard_resolver import resolve_cnpj_shard


def test_prefix_low_goes_to_shard_1() -> None:
    r = resolve_cnpj_shard("12345678000190")
    assert r.database_id == "db_shard_1"
    assert r.table_name == "recebiveis_123"


def test_prefix_boundary_499_shard_1() -> None:
    r = resolve_cnpj_shard("49900000000100")
    assert r.database_id == "db_shard_1"
    assert r.table_name == "recebiveis_499"


def test_prefix_500_goes_to_shard_2() -> None:
    r = resolve_cnpj_shard("55667788000111")
    assert r.database_id == "db_shard_2"
    assert r.table_name == "recebiveis_556"


def test_prefix_999_shard_2() -> None:
    r = resolve_cnpj_shard("99988877000155")
    assert r.database_id == "db_shard_2"
    assert r.table_name == "recebiveis_999"


def test_invalid_cnpj_raises() -> None:
    with pytest.raises(ValueError, match="14 dígitos"):
        resolve_cnpj_shard("123")
