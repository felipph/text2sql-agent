"""Testes do gerador de seed do playground."""

from __future__ import annotations

from pathlib import Path

from playground.seed_data import GABARITO, dump_sql, render_gabarito


def test_gabarito_totais() -> None:
    assert GABARITO["12345678000190"] == 175.0
    assert GABARITO["55667788000111"] == 280.0
    assert GABARITO["99988877000155"] == 40.0
    assert GABARITO["acme_beta"] == 455.0


def test_dump_sql_writes_three_files(tmp_path: Path) -> None:
    paths = dump_sql(tmp_path)
    assert {p.name for p in paths} == {"01_main.sql", "02_shard1.sql", "03_shard2.sql"}
    main = (tmp_path / "01_main.sql").read_text()
    assert "CREATE TABLE" in main and "clientes" in main and "ACME" in main
    s1 = (tmp_path / "02_shard1.sql").read_text()
    assert "recebiveis_123" in s1 and "100" in s1
    s2 = (tmp_path / "03_shard2.sql").read_text()
    assert "recebiveis_556" in s2 and "recebiveis_999" in s2


def test_render_gabarito_mentions_expected_sums() -> None:
    text = render_gabarito()
    assert "175" in text and "280" in text and "455" in text
