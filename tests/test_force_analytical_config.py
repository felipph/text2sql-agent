from pathlib import Path

from txt2sql.config import DuckDBConfig, load_config


def test_force_analytical_explicit(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db\n"
        "    connection_string: 'sqlite:///:memory:'\n"
        "tables:\n"
        "  - id: t1\n"
        "    database: db\n"
        "    name: t1\n"
        "    duckdb:\n"
        "      enabled: true\n"
        "      force_analytical: true\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.tables[0].duckdb.force_analytical is True
    assert cfg.tables[0].requires_analytical is True


def test_trigger_always_aliases_force_analytical(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db\n"
        "    connection_string: 'sqlite:///:memory:'\n"
        "tables:\n"
        "  - id: t1\n"
        "    database: db\n"
        "    name: t1\n"
        "    duckdb:\n"
        "      enabled: true\n"
        "      trigger: always\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.tables[0].duckdb.force_analytical is True
    assert cfg.tables[0].requires_analytical is True


def test_duckdb_config_force_analytical_default_false() -> None:
    assert DuckDBConfig(enabled=True).force_analytical is False
