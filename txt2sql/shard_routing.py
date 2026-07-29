from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from txt2sql.artifacts import ShardBinding, ShardRouting
from txt2sql.config import AgentConfig, ShardResult, TableConfig
from txt2sql.intent import IntentPlan


@dataclass(frozen=True)
class ClarifyNeeded:
    table_id: str
    discriminator_column: str
    question: str


def _discriminator_values(intent_plan: IntentPlan, table_id: str, disc_col: str) -> list[str]:
    """Extract discriminator values from filters (eq / in) for table+column."""
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
    return out


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


def resolve_routing(
    intent_plan: IntentPlan,
    config: AgentConfig,
    resolvers: dict[str, Callable[[str], ShardResult]] | None = None,
) -> ShardRouting | ClarifyNeeded:
    """Resolve shard bindings for sharded tables touched by the intent.

    resolvers: optional map table_id -> callable(discriminator) -> ShardResult
    If omitted, use table.sharding.load_resolver() for each sharded table.
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
        values = _discriminator_values(intent_plan, table.id, disc_col)

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
