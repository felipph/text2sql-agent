"""Tracing opcional via Langfuse (opt-in).

Compatível com Langfuse SDK v2 (``langfuse.callback``) e v3/v4
(``langfuse.langchain``). Sem pacote ou sem env vars, retorna config vazia.

No Langfuse v4, ``CallbackHandler(public_key=...)`` só funciona se um
``Langfuse()`` já estiver registrado para essa chave; caso contrário o SDK
devolve um client stub com tracing desabilitado. Por isso inicializamos o
client a partir do env antes de criar o handler.
"""

from __future__ import annotations

import inspect
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


def _import_callback_handler() -> Any | None:
    """Importa CallbackHandler (v4 ``langchain`` ou v2 ``callback``)."""
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler
    except ImportError:
        pass
    try:
        from langfuse.callback import CallbackHandler

        return CallbackHandler
    except ImportError:
        return None


def _ensure_langfuse_client() -> None:
    """Registra o client singleton Langfuse v3/v4 a partir das env vars."""
    try:
        from langfuse import get_client
    except ImportError:
        return
    # Sem public_key: cria Langfuse() a partir do env se ainda não existir.
    get_client()


def _handler_accepts_secret_key(handler_cls: Any) -> bool:
    """True para SDK v2 (kwargs completos); False para v4 (só public_key/trace)."""
    try:
        params = inspect.signature(handler_cls.__init__).parameters
    except (TypeError, ValueError):
        return False
    return "secret_key" in params


def build_tracing_callbacks(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    trace_name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[Any]:
    """Constrói callbacks Langfuse para LangGraph/LangChain.

    Em Langfuse v4, ``session_id``/tags/nome vão em ``metadata`` do config
    (``langfuse_session_id``, etc.) — use :func:`build_tracing_run_config`.

    Returns:
        Lista com o handler quando habilitado; senão lista vazia.
    """
    if not is_tracing_enabled():
        logger.debug("Tracing Langfuse desabilitado (env vars ausentes).")
        return []

    handler_cls = _import_callback_handler()
    if handler_cls is None:
        logger.warning(
            "LANGFUSE_* definidos, mas o pacote 'langfuse' não está instalado. "
            "Instale com: pip install 'txt2sql[langfuse]' "
            "(ou 'txt2sql[playground]'). Tracing desabilitado."
        )
        return []

    if _handler_accepts_secret_key(handler_cls):
        # SDK v2: credenciais e session no construtor.
        kwargs: dict[str, Any] = {
            "public_key": os.environ["LANGFUSE_PUBLIC_KEY"],
            "secret_key": os.environ["LANGFUSE_SECRET_KEY"],
            "host": os.environ.get("LANGFUSE_HOST")
            or os.environ.get("LANGFUSE_BASE_URL")
            or "https://cloud.langfuse.com",
        }
        if session_id:
            kwargs["session_id"] = session_id
        if user_id:
            kwargs["user_id"] = user_id
        if trace_name:
            kwargs["trace_name"] = trace_name
        if tags:
            kwargs["tags"] = tags
        if metadata:
            kwargs["metadata"] = metadata
        handler = handler_cls(**kwargs)
    else:
        # SDK v4: client do env primeiro; handler sem public_key (single-project).
        # Passar public_key sem client registrado → stub fake / tracing off.
        _ensure_langfuse_client()
        handler = handler_cls()

    logger.info(
        "Tracing Langfuse habilitado (session_id={}).",
        session_id,
    )
    return [handler]


def build_tracing_run_config(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    trace_name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fragmento de ``config`` LangGraph com callbacks + metadata Langfuse v4.

    Returns:
        Dict com ``callbacks`` e ``metadata`` (chaves ``langfuse_*``), ou ``{}``
        se tracing desabilitado.
    """
    callbacks = build_tracing_callbacks(
        session_id=session_id,
        user_id=user_id,
        trace_name=trace_name,
        tags=tags,
        metadata=metadata,
    )
    if not callbacks:
        return {}

    meta: dict[str, Any] = dict(metadata or {})
    if session_id:
        meta["langfuse_session_id"] = session_id
    if user_id:
        meta["langfuse_user_id"] = user_id
    if tags:
        meta["langfuse_tags"] = tags
    if trace_name:
        meta["langfuse_trace_name"] = trace_name

    return {"callbacks": callbacks, "metadata": meta}


def flush_tracing_callbacks(callbacks: list[Any] | None = None) -> None:
    """Flush handlers e client Langfuse (envia traces pendentes)."""
    for handler in callbacks or []:
        flush = getattr(handler, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception as err:  # noqa: BLE001
                logger.warning("Falha ao flush handler Langfuse: {}", err)

    try:
        from langfuse import get_client

        get_client().flush()
    except ImportError:
        return
    except Exception as err:  # noqa: BLE001
        logger.warning("Falha ao flush client Langfuse: {}", err)


__all__ = [
    "build_tracing_callbacks",
    "build_tracing_run_config",
    "flush_tracing_callbacks",
    "is_tracing_enabled",
]
