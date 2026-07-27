"""Construção do client LLM (Azure OpenAI).

A configuração pode vir do :class:`~txt2sql.config.LLMConfig` (no YAML) ou de
env vars padrão do Azure OpenAI. Valores explícitos na config têm precedência
sobre as env vars.
"""

from __future__ import annotations

import os

from langchain_openai import AzureChatOpenAI
from loguru import logger

from txt2sql.config import AgentConfig, LLMConfig


def build_llm(config: AgentConfig | LLMConfig) -> AzureChatOpenAI:
    """Constrói um client :class:`AzureChatOpenAI` a partir da configuração.

    A resolução de cada parâmetro segue a precedência: valor explícito na
    :class:`LLMConfig` > env var correspondente.

    Env vars reconhecidas:
        * ``AZURE_OPENAI_DEPLOYMENT`` (nome do deployment)
        * ``AZURE_OPENAI_MODEL`` (nome do modelo)
        * ``AZURE_OPENAI_API_VERSION``
        * ``AZURE_OPENAI_ENDPOINT``
        * ``AZURE_OPENAI_API_KEY``

    Args:
        config: Um :class:`AgentConfig` (usa ``config.llm``) ou diretamente um
            :class:`LLMConfig`.

    Returns:
        Uma instância configurada de :class:`AzureChatOpenAI`.

    Raises:
        ValueError: Se parâmetros obrigatórios (deployment, endpoint, api_key)
            não puderem ser resolvidos.
    """
    llm_cfg = config.llm if isinstance(config, AgentConfig) else config

    deployment = llm_cfg.deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
    model = llm_cfg.model or os.environ.get("AZURE_OPENAI_MODEL") or deployment
    api_version = (
        llm_cfg.api_version
        or os.environ.get("AZURE_OPENAI_API_VERSION")
        or "2024-06-01"
    )
    endpoint = llm_cfg.azure_endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = llm_cfg.api_key or os.environ.get("AZURE_OPENAI_API_KEY")

    missing = [
        name
        for name, value in (
            ("deployment", deployment),
            ("azure_endpoint", endpoint),
            ("api_key", api_key),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "Parâmetros de LLM ausentes: "
            + ", ".join(missing)
            + ". Defina no YAML (bloco 'llm') ou nas env vars AZURE_OPENAI_*."
        )

    logger.info(
        "Construindo AzureChatOpenAI (deployment={}, model={}, api_version={})",
        deployment,
        model,
        api_version,
    )

    return AzureChatOpenAI(
        azure_deployment=deployment,
        model=model,
        api_version=api_version,
        azure_endpoint=endpoint,
        api_key=api_key,
        temperature=llm_cfg.temperature,
    )


__all__ = ["build_llm"]
