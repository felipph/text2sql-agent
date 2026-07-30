from pathlib import Path

import yaml

from txt2sql.config import load_config


def test_reuse_ttl_default_1800(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        yaml.dump(
            {
                "databases": [{"id": "db", "connection_string": "sqlite://"}],
                "tables": [{"id": "t", "database": "db", "name": "t"}],
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.reuse_ttl_seconds == 1800


def test_reuse_ttl_from_analytics_yaml(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        yaml.dump(
            {
                "databases": [{"id": "db", "connection_string": "sqlite://"}],
                "tables": [{"id": "t", "database": "db", "name": "t"}],
                "analytics": {"reuse_ttl_seconds": 0},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.reuse_ttl_seconds == 0
