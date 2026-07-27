"""Gerador e aplicador de dados de teste do playground.

Uso:
    python playground/seed_data.py --dump-sql playground/seed
    python playground/seed_data.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

# --------------------------------------------------------------------------- #
# Dataset canônico (determinístico)
# --------------------------------------------------------------------------- #

CLIENTES: list[dict[str, str]] = [
    {"cnpj": "12345678000190", "razao_social": "ACME"},
    {"cnpj": "55667788000111", "razao_social": "Beta"},
    {"cnpj": "99988877000155", "razao_social": "Gama"},
]

RECEBIVEIS: list[dict[str, Any]] = [
    {
        "cnpj": "12345678000190",
        "valor": 100.0,
        "data_vencimento": "2026-01-15",
        "status": "pago",
    },
    {
        "cnpj": "12345678000190",
        "valor": 50.0,
        "data_vencimento": "2026-02-01",
        "status": "pendente",
    },
    {
        "cnpj": "12345678000190",
        "valor": 25.0,
        "data_vencimento": "2026-02-15",
        "status": "pago",
    },
    {
        "cnpj": "55667788000111",
        "valor": 200.0,
        "data_vencimento": "2026-01-20",
        "status": "pago",
    },
    {
        "cnpj": "55667788000111",
        "valor": 80.0,
        "data_vencimento": "2026-03-01",
        "status": "pendente",
    },
    {
        "cnpj": "99988877000155",
        "valor": 40.0,
        "data_vencimento": "2025-12-01",
        "status": "vencido",
    },
]


def _sum_for(cnpj: str) -> float:
    return sum(r["valor"] for r in RECEBIVEIS if r["cnpj"] == cnpj)


GABARITO: dict[str, float] = {
    "12345678000190": _sum_for("12345678000190"),
    "55667788000111": _sum_for("55667788000111"),
    "99988877000155": _sum_for("99988877000155"),
    "acme_beta": _sum_for("12345678000190") + _sum_for("55667788000111"),
}


def _physical_table(cnpj: str) -> str:
    return f"recebiveis_{cnpj[:3]}"


def _shard_key(cnpj: str) -> str:
    return "shard1" if int(cnpj[:3]) <= 499 else "shard2"


def render_gabarito() -> str:
    """Texto do gabarito para stdout / README."""
    lines = [
        "=== GABARITO (totais esperados) ===",
        f"  ACME  12345678000190  soma = {GABARITO['12345678000190']:.0f}",
        f"  Beta  55667788000111  soma = {GABARITO['55667788000111']:.0f}",
        f"  Gama  99988877000155  soma = {GABARITO['99988877000155']:.0f}",
        f"  ACME+Beta             soma = {GABARITO['acme_beta']:.0f}",
        "  Cliente com status vencido → Gama",
    ]
    return "\n".join(lines)


def _sql_main() -> str:
    inserts = "\n".join(
        f"INSERT INTO clientes (cnpj, razao_social) VALUES "
        f"('{c['cnpj']}', '{c['razao_social']}');"
        for c in CLIENTES
    )
    return f"""-- Seed playground: db_main (clientes)
CREATE TABLE IF NOT EXISTS clientes (
  cnpj VARCHAR(14) PRIMARY KEY,
  razao_social VARCHAR(200) NOT NULL
);

TRUNCATE TABLE clientes;

{inserts}
"""


def _sql_shard(shard: str) -> str:
    rows = [r for r in RECEBIVEIS if _shard_key(r["cnpj"]) == shard]
    by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_table[_physical_table(r["cnpj"])].append(r)

    parts: list[str] = [f"-- Seed playground: {shard}"]
    for table, table_rows in sorted(by_table.items()):
        parts.append(
            f"""
CREATE TABLE IF NOT EXISTS {table} (
  cnpj VARCHAR(14) NOT NULL,
  valor NUMERIC(14,2) NOT NULL,
  data_vencimento DATE NOT NULL,
  status VARCHAR(20) NOT NULL
);

TRUNCATE TABLE {table};
"""
        )
        for r in table_rows:
            parts.append(
                f"INSERT INTO {table} (cnpj, valor, data_vencimento, status) VALUES "
                f"('{r['cnpj']}', {r['valor']:.2f}, '{r['data_vencimento']}', '{r['status']}');"
            )
    return "\n".join(parts) + "\n"


def dump_sql(out_dir: Path) -> list[Path]:
    """Escreve 01_main.sql, 02_shard1.sql, 03_shard2.sql em ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [
        ("01_main.sql", _sql_main()),
        ("02_shard1.sql", _sql_shard("shard1")),
        ("03_shard2.sql", _sql_shard("shard2")),
    ]
    paths: list[Path] = []
    for name, content in files:
        path = out_dir / name
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    return paths


def _apply_sql(url: str, sql: str) -> None:
    engine = create_engine(url)
    cleaned: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        buf.append(line)
        if stripped.endswith(";"):
            cleaned.append("\n".join(buf).rstrip(";").strip())
            buf = []
    if buf:
        cleaned.append("\n".join(buf).rstrip(";").strip())

    with engine.begin() as conn:
        for stmt in cleaned:
            if stmt:
                conn.execute(text(stmt))


def apply(urls: dict[str, str]) -> None:
    """Aplica seed nos bancos ``main``, ``shard1``, ``shard2``."""
    mapping = {
        "main": _sql_main(),
        "shard1": _sql_shard("shard1"),
        "shard2": _sql_shard("shard2"),
    }
    for key, sql in mapping.items():
        if key not in urls:
            raise KeyError(f"URL ausente para {key!r}")
        _apply_sql(urls[key], sql)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed do playground txt2sql")
    parser.add_argument(
        "--dump-sql",
        nargs="?",
        const="playground/seed",
        metavar="DIR",
        help="Gera SQL em DIR (default: playground/seed)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Aplica seed nos Postgres via MAIN_DB_URL / SHARD_*_DB_URL",
    )
    args = parser.parse_args(argv)

    if not args.dump_sql and not args.apply:
        parser.print_help()
        print("\n" + render_gabarito())
        return 1

    if args.dump_sql:
        paths = dump_sql(Path(args.dump_sql))
        for p in paths:
            print(f"wrote {p}")

    if args.apply:
        urls = {
            "main": os.environ["MAIN_DB_URL"],
            "shard1": os.environ["SHARD_1_DB_URL"],
            "shard2": os.environ["SHARD_2_DB_URL"],
        }
        apply(urls)
        print("seed aplicado com sucesso")

    print(render_gabarito())
    return 0


if __name__ == "__main__":
    sys.exit(main())
