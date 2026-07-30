from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from txt2sql.artifacts import ShardBinding, ShardRouting
from txt2sql.config import AgentConfig, ShardResult, TableConfig
from txt2sql.intent import FilterClause, IntentPlan


@dataclass(frozen=True)
class ClarifyNeeded:
    table_id: str
    discriminator_column: str
    question: str


@dataclass(frozen=True)
class CapResult:
    bindings: list[ShardBinding]
    truncated: bool
    total_shards: int
    kept_shards: int
    assumption: str | None = None


def _physical_key(b: ShardBinding) -> tuple[str, str]:
    return (b.database_id, b.physical_table)


def cap_bindings_by_shards(
    bindings: list[ShardBinding],
    max_shards: int,
) -> CapResult:
    """Limita bindings pelo número de shards físicos distintos.

    Ordem estável: primeira aparição do par ``(database_id, physical_table)``.
    """
    if max_shards < 1:
        raise ValueError(f"max_shards deve ser >= 1, recebido: {max_shards}")

    order: list[tuple[str, str]] = []
    by_key: dict[tuple[str, str], list[ShardBinding]] = {}
    for b in bindings:
        key = _physical_key(b)
        if key not in by_key:
            by_key[key] = []
            order.append(key)
        by_key[key].append(b)

    total = len(order)
    if total <= max_shards:
        return CapResult(
            bindings=list(bindings),
            truncated=False,
            total_shards=total,
            kept_shards=total,
        )

    kept_keys = order[:max_shards]
    kept: list[ShardBinding] = []
    for key in kept_keys:
        kept.extend(by_key[key])
    assumption = (
        f"Cobertura parcial: {max_shards} de {total} shards físicos "
        f"(max_shards={max_shards})"
    )
    return CapResult(
        bindings=kept,
        truncated=True,
        total_shards=total,
        kept_shards=max_shards,
        assumption=assumption,
    )


def _discriminator_values_from_filters(
    intent_plan: IntentPlan,
    table_id: str,
    disc_col: str,
) -> list[str]:
    """Extrai valores do discriminador apenas de ``filters`` (eq / in)."""
    values: list[str] = []
    for f in intent_plan.filters:
        if f.table_id != table_id:
            continue
        col = f.column_id.split(".")[-1] if f.column_id else ""
        if col != disc_col and f.column_id != disc_col:
            continue
        if f.op == "eq" and f.value is not None:
            values.append(str(f.value))
        elif f.op == "in" and isinstance(f.value, list):
            values.extend(str(v) for v in f.value)
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _discriminator_values(
    intent_plan: IntentPlan,
    table_id: str,
    disc_col: str,
    *,
    extra_text: str | None = None,
    extractor: Callable[[str], list[str]] | None = None,
) -> list[str]:
    """Valores do discriminador: filters primeiro; extractor textual como fallback."""
    from_filters = _discriminator_values_from_filters(intent_plan, table_id, disc_col)
    if from_filters:
        return from_filters
    if extractor is None:
        return []
    blob = " ".join(
        part for part in (intent_plan.question_rewrite or "", extra_text or "") if part
    )
    if not blob.strip():
        return []
    raw = extractor(blob)
    seen: set[str] = set()
    out: list[str] = []
    for v in raw:
        s = str(v).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
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


def missing_discriminator_filter_errors(
    intent_plan: IntentPlan,
    config: AgentConfig,
) -> list[str]:
    """Erros estruturais: tabela shardada tocada sem filter no discriminador.

    Usado pelo nó ``interpret_intent`` para pedir retry ao LLM antes de
    clarificar o usuário. Domain-free: a coluna vem do YAML.
    """
    if intent_plan.status != "ready":
        return []
    errors: list[str] = []
    for table_id in sorted(_touched_table_ids(intent_plan)):
        table = config.try_get_table(table_id)
        if table is None or not table.is_sharded or table.sharding is None:
            continue
        disc_col = table.sharding.discriminator_column
        if _discriminator_values_from_filters(intent_plan, table_id, disc_col):
            continue
        errors.append(
            f"Tabela shardada {table_id!r} exige FilterClause em "
            f"filters com column_id={disc_col!r} (op=eq ou op=in) quando o "
            f"valor do discriminador já estiver na pergunta ou no histórico. "
            f"Não deixe o valor só em question_rewrite."
        )
    return errors


def ensure_discriminator_filters(
    intent_plan: IntentPlan,
    routing: ShardRouting,
    config: AgentConfig,
) -> IntentPlan:
    """Garante FilterClause(eq/in) para discriminadores presentes nos bindings."""
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
        existing = _discriminator_values_from_filters(
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
    extractors: dict[str, Callable[[str], list[str]]] | None = None,
    extra_text: str | None = None,
    registry: Any | None = None,
) -> ShardRouting | ClarifyNeeded:
    """Resolve shard bindings for sharded tables touched by the intent.

    resolvers: optional map table_id -> callable(discriminator) -> ShardResult
    extractors: optional map table_id -> callable(text) -> list[str] (fallback)
    If omitted, load from ``ShardingConfig`` on each table.
    registry: se fornecido, valida ``database_id`` via ``has_database``.
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

    for table in sharded_touched:
        assert table.sharding is not None
        disc_col = table.sharding.discriminator_column

        extractor: Callable[[str], list[str]] | None = None
        if extractors is not None and table.id in extractors:
            extractor = extractors[table.id]
        else:
            extractor = table.sharding.load_value_extractor()

        values = _discriminator_values(
            intent_plan,
            table.id,
            disc_col,
            extra_text=extra_text,
            extractor=extractor,
        )

        if not values:
            return ClarifyNeeded(
                table_id=table.id,
                discriminator_column=disc_col,
                question=_clarify_question(table, disc_col),
            )

        if resolvers is not None and table.id in resolvers:
            resolve_fn = resolvers[table.id]
        else:
            resolve_fn = table.sharding.load_resolver()

        for value in values:
            result = resolve_fn(value)
            if not isinstance(result, ShardResult):
                raise TypeError(
                    f"Resolver de {table.id!r} deve retornar ShardResult, "
                    f"retornou {type(result).__name__}."
                )
            if registry is not None and not registry.has_database(result.database_id):
                raise ValueError(
                    f"Resolver de {table.id!r} retornou database_id inexistente: "
                    f"{result.database_id!r}."
                )
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

    capped = cap_bindings_by_shards(all_bindings, config.max_shards)
    all_bindings = capped.bindings
    # Recompute multi after cap
    disc_counts: dict[str, set[str]] = {}
    for b in all_bindings:
        disc_counts.setdefault(b.table_id, set()).add(b.discriminator_value)
    total_values = sum(len(v) for v in disc_counts.values())
    multi_table_id = None
    for tid, vals in disc_counts.items():
        if len(vals) >= 2:
            multi_table_id = tid
            break

    if total_values >= 2 or multi_table_id is not None:
        logical = multi_table_id or sharded_touched[0].id
        return ShardRouting(
            mode="multi",
            bindings=all_bindings,
            logical_table=logical,
            capped=capped.truncated,
            cap_assumption=capped.assumption,
        )

    return ShardRouting(
        mode="single",
        bindings=all_bindings,
        capped=capped.truncated,
        cap_assumption=capped.assumption,
    )
