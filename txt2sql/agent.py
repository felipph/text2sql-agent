"""Entrypoint público do agente Text-to-SQL.

Delega para :mod:`txt2sql.graph` que implementa o grafo dual-path
(simple | analytical).
"""

from __future__ import annotations

from typing import Any

from langgraph.graph.state import CompiledStateGraph

from txt2sql.config import AgentConfig


def build_agent(
    config: AgentConfig,
    checkpointer: Any | None = None,
) -> CompiledStateGraph:
    """Constrói e compila o grafo LangGraph do agente Text-to-SQL.

    Args:
        config: Configuração do agente.
        checkpointer: Checkpointer externo do LangGraph (opcional). A biblioteca
            NÃO gerencia checkpointer internamente — é responsabilidade do caller
            fornecer um (ex.: ``MemorySaver`` ou ``AsyncPostgresSaver``).

    Returns:
        O grafo compilado, pronto para ``invoke``/``stream``.
    """
    from txt2sql.graph import build_graph

    return build_graph(config, checkpointer=checkpointer)


__all__ = ["build_agent"]
