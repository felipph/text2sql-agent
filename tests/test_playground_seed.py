"""Testes do gerador paramétrico de seed do playground."""

from __future__ import annotations

from pathlib import Path

import yaml

from playground.seed_data import (
    SeedParams,
    dump_sql,
    generate_dataset,
    load_params,
    render_gabarito,
    write_prompts,
)


def test_generate_deterministic_same_seed() -> None:
    p = SeedParams(cnpjs=3, por_cnpj=2, seed=42)
    a = generate_dataset(p)
    b = generate_dataset(p)
    assert a.clientes == b.clientes
    assert a.recebiveis == b.recebiveis
    assert len(a.clientes) == 3
    assert len(a.recebiveis) == 6


def test_generate_counts_and_unique_cnpjs() -> None:
    ds = generate_dataset(SeedParams(cnpjs=5, por_cnpj=4, seed=7))
    assert len(ds.clientes) == 5
    assert len(ds.recebiveis) == 20
    cnpjs = [c["cnpj"] for c in ds.clientes]
    assert len(set(cnpjs)) == 5
    assert all(len(c) == 14 and c.isdigit() for c in cnpjs)


def test_generate_guarantees_vencido() -> None:
    ds = generate_dataset(SeedParams(cnpjs=2, por_cnpj=1, seed=1))
    assert any(r["status"] == "vencido" for r in ds.recebiveis)


def test_random_flag_differs_from_seeded() -> None:
    seeded = generate_dataset(SeedParams(cnpjs=4, por_cnpj=2, seed=99))
    # seed=None → RNG do sistema; rode algumas vezes se colidir (improvável com 4 CNPJs)
    other = generate_dataset(SeedParams(cnpjs=4, por_cnpj=2, seed=None))
    assert [c["cnpj"] for c in seeded.clientes] != [c["cnpj"] for c in other.clientes]


def test_dump_sql_and_prompts(tmp_path: Path) -> None:
    ds = generate_dataset(SeedParams(cnpjs=3, por_cnpj=2, seed=42))
    paths = dump_sql(ds, tmp_path / "seed")
    assert {p.name for p in paths} == {"01_main.sql", "02_shard1.sql", "03_shard2.sql"}
    main = (tmp_path / "seed" / "01_main.sql").read_text()
    assert "Cliente_000" in main and "CREATE TABLE" in main

    prompts_path = tmp_path / "prompts.yaml"
    write_prompts(ds, prompts_path)
    data = yaml.safe_load(prompts_path.read_text())
    ids = [p["id"] for p in data["prompts"]]
    assert ids == ["single_sum", "multi_sum", "join_vencido", "guardrail_delete"]
    single = data["prompts"][0]
    assert single["expected"]
    assert ds.clientes[0]["cnpj"] in single["question"]


def test_load_params_overrides(tmp_path: Path) -> None:
    cfg = tmp_path / "seed_params.yaml"
    cfg.write_text("cnpjs: 10\npor_cnpj: 5\nseed: 1\n", encoding="utf-8")
    p = load_params(cfg, overrides={"cnpjs": 2, "random": True})
    assert p.cnpjs == 2
    assert p.por_cnpj == 5
    assert p.seed is None


def test_render_gabarito_lists_clients() -> None:
    ds = generate_dataset(SeedParams(cnpjs=2, por_cnpj=2, seed=3))
    text = render_gabarito(ds)
    assert ds.clientes[0]["cnpj"] in text
    assert "soma" in text.lower() or "=" in text
