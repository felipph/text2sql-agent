"""txt2sql — biblioteca standalone para agentes Text-to-SQL com LangGraph.

Recursos principais:
    * Multi-banco com sharding determinístico (sem fan-out).
    * Schema declarativo (YAML) OU discovery automático (SQLAlchemy).
    * Camada DuckDB intermediária efêmera por turno para tabelas volumétricas.
    * Guardrail read-only fail-closed via AST do sqlglot.

API pública:
    >>> from txt2sql import build_agent, load_config, AgentConfig, ShardResult, QueryTimeoutError
    >>> config = load_config("examples/recebiveis.yaml")
    >>> agent = build_agent(config)  # aceita checkpointer externo opcional
"""

from __future__ import annotations

from txt2sql.agent import build_agent
from txt2sql.config import AgentConfig, ExportConfig, ShardResult, load_config
from txt2sql.db.registry import QueryTimeoutError
from txt2sql.export_csv import cleanup_expired_exports

__all__ = [
    "AgentConfig",
    "ExportConfig",
    "QueryTimeoutError",
    "ShardResult",
    "build_agent",
    "cleanup_expired_exports",
    "load_config",
]

__version__ = "0.1.0"
