"""Testes dos knobs de agent/analytics (budget, messages, breaking keys)."""

from pathlib import Path

import pytest
import yaml

from txt2sql.config import load_config


def _minimal(extra_agent: dict | None = None, analytics: dict | None = None) -> dict:
    raw: dict = {
        "databases": [{"id": "db", "connection_string": "sqlite://"}],
        "tables": [{"id": "t", "database": "db", "name": "t"}],
    }
    if extra_agent is not None:
        raw["agent"] = extra_agent
    if analytics is not None:
        raw["analytics"] = analytics
    return raw


def _write(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.dump(raw), encoding="utf-8")
    return path


@pytest.mark.parametrize("key", ["top_k", "max_pages", "sample_rows_in_table_info"])
def test_removed_agent_keys_fail_closed(tmp_path: Path, key: str) -> None:
    path = _write(tmp_path, _minimal({key: 10}))
    with pytest.raises(ValueError, match="Campos removidos"):
        load_config(path)


def test_budget_and_limits_from_yaml(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _minimal(
            {
                "sample_rows": 7,
                "query_max_rows": 1234,
                "max_intent_retries": 4,
                "budget": {
                    "max_clarifications": 5,
                    "max_refine": 6,
                    "max_mat_loops": 7,
                    "max_gate_visits": 8,
                    "max_rows_per_extract": 999,
                    "max_rows_materialized": 1111,
                },
                "messages": {"clarification_exhausted": "ACABOU"},
                "prompts": {"intent_extra": "EXTRA INTENT"},
                "export_detect_keywords": ["csv"],
            },
            analytics={"batch_size": 100, "materialize_sample_rows": 2},
        ),
    )
    cfg = load_config(path)
    assert cfg.sample_rows == 7
    assert cfg.query_max_rows == 1234
    assert cfg.max_intent_retries == 4
    assert cfg.budget.max_clarifications == 5
    assert cfg.budget.max_refine == 6
    assert cfg.budget.max_rows_per_extract == 999
    assert cfg.messages.clarification_exhausted == "ACABOU"
    assert cfg.prompts.intent_extra == "EXTRA INTENT"
    assert cfg.export_detect_keywords == ["csv"]
    assert cfg.batch_size == 100
    assert cfg.materialize_sample_rows == 2


def test_defaults_when_omitted(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _minimal()))
    assert cfg.sample_rows == 20
    assert cfg.query_max_rows == 500_000
    assert cfg.budget.max_clarifications == 2
    assert cfg.batch_size == 5_000
    assert cfg.export_detect_keywords is None
    assert "esclarecimentos" in cfg.messages.clarification_exhausted.lower()
