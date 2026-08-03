"""Helpers de messages / discriminator / intent_extra."""

from txt2sql.answer_grounding import (
    build_partial_user_notice,
    resolve_discriminator_label,
)
from txt2sql.config import (
    AgentConfig,
    ColumnConfig,
    DatabaseConfig,
    MessagesConfig,
    PromptsConfig,
    ShardingConfig,
    TableConfig,
)
from txt2sql.intent import EntityRef, IntentPlan
from txt2sql.prompts import Txt2SqlPromptBuilder


def _cfg() -> AgentConfig:
    return AgentConfig(
        databases=[DatabaseConfig(id="db", connection_string="sqlite://")],
        tables=[
            TableConfig(
                id="recebiveis",
                database="db",
                name="recebiveis",
                columns=[ColumnConfig(name="cnpj")],
                sharding=ShardingConfig(
                    discriminator_column="cnpj",
                    resolver="examples.shard_resolver_example:resolve_cnpj_shard",
                ),
            )
        ],
        messages=MessagesConfig(
            partial_coverage="Refine por {discriminator}.",
        ),
        prompts=PromptsConfig(intent_extra="EXTRA_INTENT_MARKER"),
    )


def test_resolve_discriminator_from_sharded_table() -> None:
    cfg = _cfg()
    plan = IntentPlan(
        status="ready",
        entities=[EntityRef(mention="r", table_id="recebiveis", role="table")],
    )
    assert resolve_discriminator_label(cfg, plan) == "cnpj"


def test_partial_notice_uses_discriminator_template() -> None:
    notice = build_partial_user_notice(
        partial=True,
        max_shards=2,
        discriminator="cnpj",
        suggestion_template="Refine por {discriminator}.",
    )
    assert notice is not None
    assert "cnpj" in notice
    assert "CNPJ" not in notice or "cnpj" in notice.lower()


def test_intent_extra_in_prompt() -> None:
    cfg = _cfg()
    text = Txt2SqlPromptBuilder(cfg).build_intent_prompt()
    assert "EXTRA_INTENT_MARKER" in text


def test_sql_prompt_forbids_dml() -> None:
    cfg = _cfg()
    text = Txt2SqlPromptBuilder(cfg).build()
    assert "NUNCA gere" in text or "SOMENTE consultas de leitura" in text
    assert "INSERT" in text
