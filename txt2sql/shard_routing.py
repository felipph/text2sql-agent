from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from txt2sql.artifacts import ShardBinding, ShardRouting
from txt2sql.config import AgentConfig, ShardResult, TableConfig
from txt2sql.intent import FilterClause, IntentPlan


@dataclass(frozen=True)
class ClarifyNeeded:
    table_id: str
    discriminator_column: str
    question: str


_CNPJ_RE = re.compile(r"\b(\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}|\d{14})\b")
_CPF_RE = re.compile(r"\b(\d{3}\.?\d{3}\.?\d{3}-?\d{2}|\d{11})\b")


def _normalize_digits(raw: str) -> str:
    return re.sub(r"\D", "", raw)


def extract_discriminator_candidates(text: str, disc_col: str) -> list[str]:
    """Extrai candidatos a discriminador de texto livre (rewrite / mensagem).

    Fallback determinístico quando o IntentPlan omite o valor em ``filters``.
    Hoje cobre CNPJ/CPF (identificadores numéricos do domínio); outras colunas
    retornam lista vazia (clarificação permanece).
    """
    if not text or not disc_col:
        return []
    col = disc_col.split(".")[-1].lower()
    seen: set[str] = set()
    out: list[str] = []

    if col == "cnpj":
        pattern, length = _CNPJ_RE, 14
    elif col == "cpf":
        pattern, length = _CPF_RE, 11
    else:
        return []

    for match in pattern.finditer(text):
        digits = _normalize_digits(match.group(1))
        if len(digits) != length or digits in seen:
            continue
        seen.add(digits)
        out.append(digits)
    return out


def _discriminator_values(
    intent_plan: IntentPlan,
    table_id: str,
    disc_col: str,
    *,
    extra_text: str | None = None,
) -> list[str]:
    """Extract discriminator values from filters (eq / in), then text fallback."""
    values: list[str] = []
    for f in intent_plan.filters:
        if f.table_id != table_id:
            continue
        # column_id may be "cnpj" or "recebiveis.cnpj" style — match disc_col or endswith
        col = f.column_id.split(".")[-1] if f.column_id else ""
        if col != disc_col and f.column_id != disc_col:
            continue
        if f.op == "eq" and f.value is not None:
            values.append(str(f.value))
        elif f.op == "in" and isinstance(f.value, list):
            values.extend(str(v) for v in f.value)
    # preserve order, unique
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    if out:
        return out

    # Fallback: question_rewrite + mensagem do usuário
    blob = " ".join(
        part for part in (intent_plan.question_rewrite or "", extra_text or "") if part
    )
    return extract_discriminator_candidates(blob, disc_col)


def _touched_table_ids(intent_plan: IntentPlan) -> set[str]:
    tables: set[str] = set()
    for f in intent_plan.filters:
        tables.add(f.table_id)
    for m in intent_plan.metrics:
        tables.add(m.table_id)
    for g in intent_plan.group_by:
        tables.add(g.table_id)
    for o in intent_plan.order_by:
        tables.add(o.table_id)
    for j in intent_plan.joins:
        tables.add(j.from_table_id)
        tables.add(j.to_table_id)
    for e in intent_plan.entities:
        if e.table_id:
            tables.add(e.table_id)
    return tables


def _clarify_question(table: TableConfig, disc_col: str) -> str:
    label = disc_col.upper() if disc_col.isalpha() and len(disc_col) <= 5 else disc_col
    name = table.name or table.id
    return f"Para consultar a tabela {name!r}, informe o valor de {label} (coluna {disc_col})."


def ensure_discriminator_filters(
    intent_plan: IntentPlan,
    routing: ShardRouting,
    config: AgentConfig,
) -> IntentPlan:
    """Garante FilterClause(eq/in) para discriminadores presentes nos bindings.

    Quando o fallback textual resolveu o shard, o plano pode ainda estar sem
    ``filters`` — enriquecer evita SQL sem WHERE no discriminador.
    """
    if not routing.bindings:
        return intent_plan

    by_table: dict[str, list[str]] = {}
    for b in routing.bindings:
        by_table.setdefault(b.table_id, []).append(b.discriminator_value)

    new_filters = list(intent_plan.filters)
    changed = False
    for table_id, values in by_table.items():
        table = config.try_get_table(table_id)
        if table is None or table.sharding is None:
            continue
        disc_col = table.sharding.discriminator_column
        existing = _discriminator_values(
            IntentPlan(filters=new_filters), table_id, disc_col
        )
        if existing:
            continue
        unique = list(dict.fromkeys(values))
        if len(unique) == 1:
            new_filters.append(
                FilterClause(
                    table_id=table_id, column_id=disc_col, op="eq", value=unique[0]
                )
            )
        else:
            new_filters.append(
                FilterClause(
                    table_id=table_id, column_id=disc_col, op="in", value=unique
                )
            )
        changed = True

    if not changed:
        return intent_plan
    return intent_plan.model_copy(update={"filters": new_filters})


def resolve_routing(
    intent_plan: IntentPlan,
    config: AgentConfig,
    resolvers: dict[str, Callable[[str], ShardResult]] | None = None,
    *,
    extra_text: str | None = None,
) -> ShardRouting | ClarifyNeeded:
    """Resolve shard bindings for sharded tables touched by the intent.

    resolvers: optional map table_id -> callable(discriminator) -> ShardResult
    If omitted, use table.sharding.load_resolver() for each sharded table.

    extra_text: mensagem do usuário (ou outro texto) para fallback quando o
    discriminador não está em ``filters`` mas aparece no texto.
    """
    touched = _touched_table_ids(intent_plan)
    if not touched:
        return ShardRouting(mode="none")

    sharded_touched: list[TableConfig] = []
    for table_id in sorted(touched):
        table = config.try_get_table(table_id)
        if table is not None and table.is_sharded and table.sharding is not None:
            sharded_touched.append(table)

    if not sharded_touched:
        return ShardRouting(mode="none")

    all_bindings: list[ShardBinding] = []
    total_values = 0
    multi_table_id: str | None = None

    for table in sharded_touched:
        assert table.sharding is not None
        disc_col = table.sharding.discriminator_column
        values = _discriminator_values(
            intent_plan, table.id, disc_col, extra_text=extra_text
        )

        if not values:
            return ClarifyNeeded(
                table_id=table.id,
                discriminator_column=disc_col,
                question=_clarify_question(table, disc_col),
            )

        # Trunca como materialize_sharded_table quando excede o cap configurado.
        if len(values) > config.max_shard_discriminators:
            values = values[: config.max_shard_discriminators]

        if len(values) >= 2:
            multi_table_id = table.id

        total_values += len(values)

        if resolvers is not None and table.id in resolvers:
            resolve_fn = resolvers[table.id]
        else:
            resolve_fn = table.sharding.load_resolver()

        for value in values:
            result = resolve_fn(value)
            all_bindings.append(
                ShardBinding(
                    table_id=table.id,
                    discriminator_value=value,
                    database_id=result.database_id,
                    physical_table=result.table_name,
                )
            )

    if not all_bindings:
        return ShardRouting(mode="none")

    if total_values >= 2 or multi_table_id is not None:
        logical = multi_table_id or sharded_touched[0].id
        return ShardRouting(mode="multi", bindings=all_bindings, logical_table=logical)

    return ShardRouting(mode="single", bindings=all_bindings)
