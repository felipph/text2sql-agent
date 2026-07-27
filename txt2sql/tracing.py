"""Tracing opcional via Langfuse (opt-in).

O Langfuse é uma dependência opcional. Se não estiver instalado ou não
configurado por env vars, :func:`build_tracing_callbacks` retorna uma lista
vazia — o agente funciona normalmente sem tracing.
"""

from __future__ import annotations

import os
from typing import Any

from loguru import logger


def is_tracing_enabled() -> bool:
    """Indica se o tracing Langfuse deve ser habilitado.

    Habilitado quando ``LANGFUSE_PUBLIC_KEY`` e ``LANGFUSE_SECRET_KEY`` estão
    definidos no ambiente.
    """
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
    )


def build_tracing_callbacks() -> list[Any]:
    """Constrói a lista de callbacks de tracing para o LangGraph/LangChain.

    Returns:
        Lista com o ``CallbackHandler`` do Langfuse quando habilitado e
        disponível; caso contrário, lista vazia.
    """
    if not is_tracing_enabled():
        logger.debug("Tracing Langfuse desabilitado (env vars ausentes).")
        return []

    try:
        from langfuse.callback import CallbackHandler
    except ImportError:
        logger.warning(
            "LANGFUSE_* definidos, mas o pacote 'langfuse' não está instalado. "
            "Instale com: pip install 'txt2sql[langfuse]'. Tracing desabilitado."
        )
        return []

    handler = CallbackHandler(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
    )
    logger.info("Tracing Langfuse habilitado.")
    return [handler]


__all__ = ["is_tracing_enabled", "build_tracing_callbacks"]
