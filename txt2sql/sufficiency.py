"""Sufficiency gate determinístico: cobre IntentPlan + shards + AST + TTL."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp

from txt2sql.artifacts import (
    DuckDBCatalog,
    DuckDBTableInfo,
    MaterializationPlan,
    MaterializationStep,
    ShardBinding,
    ShardRouting,
)
from txt2sql.config import AgentConfig
from txt2sql.intent import FilterClause, IntentPlan

_FAN_IN_RE = re.compile(r"^fan-in:\d+ bindings$", re.IGNORECASE)


class TableGap(BaseModel):
    table_id: str
    reason: Literal[
        "missing_table",
        "missing_shard",
        "missing_columns",
        "predicate_mismatch",
        "stale",
    ]
    missing_bindings: list[ShardBinding] = Field(default_factory=list)
    missing_columns: list[str] = Field(default_factory=list)
    detail: str = ""


class SufficiencyDecision(BaseModel):
    action: Literal["reuse", "refresh", "unknown"]
    gaps: list[TableGap] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


def intent_table_ids(plan: IntentPlan) -> set[str]:
    """Tabelas lógicas tocadas pelo IntentPlan."""
    ids: set[str] = set()
    for f in plan.filters:
        ids.add(f.table_id)
    for m in plan.metrics:
        ids.add(m.table_id)
    for g in plan.group_by:
        ids.add(g.table_id)
    for j in plan.joins:
        ids.add(j.from_table_id)
        ids.add(j.to_table_id)
    for e in plan.entities:
        if e.table_id:
            ids.add(e.table_id)
    for o in plan.order_by:
        ids.add(o.table_id)
    return ids


def _catalog_entry(
    catalog: DuckDBCatalog,
    table_id: str,
    config: AgentConfig,
) -> DuckDBTableInfo | None:
    names = {table_id.lower()}
    table = config.try_get_table(table_id)
    if table is not None:
        names.add(table.name.lower())
        names.add(table.id.lower())
    for info in catalog.tables:
        if info.name.lower() in names:
            return info
    return None


def _binding_key(b: ShardBinding) -> tuple[str, str]:
    return (b.database_id, b.discriminator_value)


def _missing_shard_bindings(
    required: list[ShardBinding],
    cached: list[ShardBinding],
) -> list[ShardBinding]:
    have = {_binding_key(b) for b in cached}
    return [b for b in required if _binding_key(b) not in have]


def _intent_columns_for_table(intent: IntentPlan, table_id: str) -> set[str]:
    cols: set[str] = set()
    for f in intent.filters:
        if f.table_id == table_id:
            cols.add(f.column_id.lower())
    for m in intent.metrics:
        if m.table_id == table_id and m.column_id:
            cols.add(m.column_id.lower())
    for g in intent.group_by:
        if g.table_id == table_id:
            cols.add(g.column_id.lower())
    for o in intent.order_by:
        if o.table_id == table_id:
            cols.add(o.column_id.lower())
    for j in intent.joins:
        if j.from_table_id == table_id:
            for on in j.on:
                cols.add(on.from_column.lower())
        if j.to_table_id == table_id:
            for on in j.on:
                cols.add(on.to_column.lower())
    return cols


def _select_root(parsed: exp.Expression) -> exp.Select | None:
    if isinstance(parsed, exp.Select):
        return parsed
    return next(parsed.find_all(exp.Select), None)


def _is_star_projection(root: exp.Select) -> bool:
    for proj in root.expressions:
        if isinstance(proj, exp.Star):
            return True
        if isinstance(proj, exp.Alias) and isinstance(proj.this, exp.Star):
            return True
    return False


def _is_synthetic_query(sql: str) -> bool:
    return bool(_FAN_IN_RE.match(sql.strip()))


def _parse_select(sql: str, dialect: str | None) -> exp.Select | None:
    try:
        parsed = sqlglot.parse_one(sql.strip(), dialect=dialect)
    except Exception:  # noqa: BLE001 — fail-closed → unknown
        return None
    return _select_root(parsed)


def _projected_columns(sql: str, dialect: str | None) -> set[str] | None:
    """None = unknown; set vazio especial via caller para star/synthetic.

    Retorna o conjunto de nomes projetados, ou None se não mapeável.
    Star/synthetic devem ser tratados antes via ``_column_coverage``.
    """
    root = _parse_select(sql, dialect)
    if root is None:
        return None
    if _is_star_projection(root):
        return set()  # caller: covers_all
    cols: set[str] = set()
    for proj in root.expressions:
        if isinstance(proj, exp.Alias):
            cols.add(proj.alias_or_name.lower())
            continue
        if isinstance(proj, exp.Column):
            cols.add(proj.name.lower())
            continue
        return None
    return cols


def _column_coverage(
    source_queries: list[str],
    needed: set[str],
    dialect: str | None,
) -> Literal["ok", "gap", "unknown"] | tuple[Literal["gap"], list[str]]:
    if not needed:
        return "ok"
    covers_all = False
    covered: set[str] = set()
    for q in source_queries or []:
        if _is_synthetic_query(q):
            covers_all = True
            break
        root = _parse_select(q, dialect)
        if root is None:
            return "unknown"
        if _is_star_projection(root):
            covers_all = True
            break
        proj = _projected_columns(q, dialect)
        if proj is None:
            return "unknown"
        covered |= proj
    if covers_all:
        return "ok"
    missing = sorted(needed - covered)
    if missing:
        return ("gap", missing)
    return "ok"


@dataclass
class _ColConstraint:
    eq_values: set[str] | None = None
    lower: tuple[float, bool] | None = None  # (bound, inclusive)
    upper: tuple[float, bool] | None = None


@dataclass
class _PredResult:
    status: Literal["ok", "mismatch", "unknown"]
    constraints: dict[str, _ColConstraint] = field(default_factory=dict)


def _lit_str(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Literal):
        return str(node.this)
    if isinstance(node, exp.Boolean):
        return str(node.this).lower()
    if isinstance(node, exp.Null):
        return "null"
    return None


def _lit_num(node: exp.Expression) -> float | None:
    s = _lit_str(node)
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _col_name(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Column):
        return node.name.lower()
    return None


def _merge_eq(dst: _ColConstraint, values: set[str]) -> Literal["ok", "unknown"]:
    if dst.eq_values is None:
        dst.eq_values = set(values)
        return "ok"
    dst.eq_values &= values
    return "ok"


def _apply_cmp(
    dst: _ColConstraint,
    col_on_left: bool,
    op: type[exp.Expression],
    lit: exp.Expression,
) -> Literal["ok", "unknown"]:
    num = _lit_num(lit)
    if num is None:
        # equality with non-numeric literal
        if op is exp.EQ:
            s = _lit_str(lit)
            if s is None:
                return "unknown"
            return _merge_eq(dst, {s})
        return "unknown"

    # Normalize so constraint is always on the column side
    # col ? lit  vs  lit ? col
    if op is exp.GT:
        # col > lit → lower exclusive; lit > col → upper exclusive
        if col_on_left:
            dst.lower = _max_lower(dst.lower, (num, False))
        else:
            dst.upper = _min_upper(dst.upper, (num, False))
        return "ok"
    if op is exp.GTE:
        if col_on_left:
            dst.lower = _max_lower(dst.lower, (num, True))
        else:
            dst.upper = _min_upper(dst.upper, (num, True))
        return "ok"
    if op is exp.LT:
        if col_on_left:
            dst.upper = _min_upper(dst.upper, (num, False))
        else:
            dst.lower = _max_lower(dst.lower, (num, False))
        return "ok"
    if op is exp.LTE:
        if col_on_left:
            dst.upper = _min_upper(dst.upper, (num, True))
        else:
            dst.lower = _max_lower(dst.lower, (num, True))
        return "ok"
    if op is exp.EQ:
        return _merge_eq(dst, {str(num) if num != int(num) else str(int(num))})
    return "unknown"


def _max_lower(cur: tuple[float, bool] | None, new: tuple[float, bool]) -> tuple[float, bool]:
    if cur is None:
        return new
    if new[0] > cur[0]:
        return new
    if new[0] < cur[0]:
        return cur
    # same bound: exclusive is stricter
    return (cur[0], cur[1] and new[1])


def _min_upper(cur: tuple[float, bool] | None, new: tuple[float, bool]) -> tuple[float, bool]:
    if cur is None:
        return new
    if new[0] < cur[0]:
        return new
    if new[0] > cur[0]:
        return cur
    return (cur[0], cur[1] and new[1])


def _ingest_predicate(
    node: exp.Expression, out: dict[str, _ColConstraint]
) -> Literal["ok", "unknown"]:
    if isinstance(node, exp.Paren):
        return _ingest_predicate(node.this, out)
    if isinstance(node, exp.And):
        left = _ingest_predicate(node.left, out)
        right = _ingest_predicate(node.right, out)
        if left == "unknown" or right == "unknown":
            return "unknown"
        return "ok"
    if isinstance(node, exp.Or):
        return "unknown"
    if isinstance(node, exp.Not):
        return "unknown"
    if isinstance(node, (exp.Like, exp.ILike)):
        return "unknown"

    if isinstance(node, exp.Between):
        col = _col_name(node.this)
        if col is None:
            return "unknown"
        low = _lit_num(node.args["low"])
        high = _lit_num(node.args["high"])
        if low is None or high is None:
            return "unknown"
        c = out.setdefault(col, _ColConstraint())
        c.lower = _max_lower(c.lower, (low, True))
        c.upper = _min_upper(c.upper, (high, True))
        return "ok"

    if isinstance(node, exp.In):
        col = _col_name(node.this)
        if col is None:
            return "unknown"
        values: set[str] = set()
        for v in node.expressions:
            s = _lit_str(v)
            if s is None:
                return "unknown"
            values.add(s)
        c = out.setdefault(col, _ColConstraint())
        return _merge_eq(c, values)

    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        if isinstance(node, exp.NEQ):
            return "unknown"
        left, right = node.left, node.right
        col_l, col_r = _col_name(left), _col_name(right)
        if col_l and _lit_str(right) is not None:
            c = out.setdefault(col_l, _ColConstraint())
            return _apply_cmp(c, True, type(node), right)
        if col_r and _lit_str(left) is not None:
            c = out.setdefault(col_r, _ColConstraint())
            return _apply_cmp(c, False, type(node), left)
        return "unknown"

    return "unknown"


def _extract_where_constraints(sql: str, dialect: str | None) -> _PredResult:
    if _is_synthetic_query(sql):
        return _PredResult(status="ok")
    root = _parse_select(sql, dialect)
    if root is None:
        return _PredResult(status="unknown")
    where = root.find(exp.Where)
    if where is None:
        return _PredResult(status="ok")
    out: dict[str, _ColConstraint] = {}
    status = _ingest_predicate(where.this, out)
    if status == "unknown":
        return _PredResult(status="unknown")
    return _PredResult(status="ok", constraints=out)


def _value_as_str(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _intent_constraint(f: FilterClause) -> _ColConstraint | Literal["unknown"]:
    if f.op in {"like", "ne", "is_null"}:
        return "unknown"
    c = _ColConstraint()
    if f.op == "eq":
        c.eq_values = {_value_as_str(f.value)}
        return c
    if f.op == "in":
        if not isinstance(f.value, list):
            return "unknown"
        c.eq_values = {_value_as_str(v) for v in f.value}
        return c
    if f.op == "between":
        if not isinstance(f.value, list) or len(f.value) != 2:
            return "unknown"
        try:
            lo, hi = float(f.value[0]), float(f.value[1])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return "unknown"
        c.lower = (lo, True)
        c.upper = (hi, True)
        return c
    try:
        num = float(f.value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unknown"
    if f.op == "gt":
        c.lower = (num, False)
    elif f.op == "gte":
        c.lower = (num, True)
    elif f.op == "lt":
        c.upper = (num, False)
    elif f.op == "lte":
        c.upper = (num, True)
    else:
        return "unknown"
    return c


def _range_contained(inner: _ColConstraint, outer: _ColConstraint) -> bool:
    """True se o conjunto de valores de ``inner`` ⊆ ``outer`` (ranges)."""
    if outer.eq_values is not None:
        # outer is discrete set
        if inner.eq_values is not None:
            return inner.eq_values <= outer.eq_values
        return False  # range cannot be proven ⊆ discrete set conservatively

    if inner.eq_values is not None:
        for v in inner.eq_values:
            try:
                num = float(v)
            except ValueError:
                return False
            if outer.lower is not None:
                lo, inc = outer.lower
                if num < lo or (num == lo and not inc):
                    return False
            if outer.upper is not None:
                hi, inc = outer.upper
                if num > hi or (num == hi and not inc):
                    return False
        return True

    # both ranges: inner.lower >= outer.lower and inner.upper <= outer.upper
    if outer.lower is not None:
        if inner.lower is None:
            return False
        ilo, iinc = inner.lower
        olo, oinc = outer.lower
        if ilo < olo:
            return False
        if ilo == olo and iinc and not oinc:
            return False
    if outer.upper is not None:
        if inner.upper is None:
            return False
        ihi, iinc = inner.upper
        ohi, oinc = outer.upper
        if ihi > ohi:
            return False
        if ihi == ohi and iinc and not oinc:
            return False
    return True


def _predicates_cover(
    intent: IntentPlan,
    table_id: str,
    source_queries: list[str],
    dialect: str | None,
) -> Literal["ok", "mismatch", "unknown"]:
    """Reuse seguro sse G ⇒ F (intent ⊆ extract): cache mais estreito que o intent → mismatch."""
    queries = list(source_queries or [])
    if not queries:
        # sem proveniência de query → fail-closed unknown (não reuse cego)
        return "unknown"

    non_synthetic = [q for q in queries if not _is_synthetic_query(q)]
    if len(non_synthetic) > 1:
        return "unknown"

    # Todas sintéticas → cobrem predicados de domínio
    if not non_synthetic:
        return "ok"

    extracted = _extract_where_constraints(non_synthetic[0], dialect)
    if extracted.status == "unknown":
        return "unknown"
    cache_preds = extracted.constraints

    intent_preds: dict[str, _ColConstraint] = {}
    for f in intent.filters:
        if f.table_id != table_id:
            continue
        intent_c = _intent_constraint(f)
        if intent_c == "unknown":
            return "unknown"
        col = f.column_id.lower()
        # múltiplos filtros na mesma coluna: exige contido em ambos (interseção)
        if col in intent_preds:
            # conservador: se já há constraint, trata como unknown (AND complexo)
            return "unknown"
        intent_preds[col] = intent_c

    # Toda restrição do extract precisa ser implicada pelo intent (G ⇒ F)
    for col, outer in cache_preds.items():
        inner = intent_preds.get(col)
        if inner is None:
            # extract restringe coluna que o intent não restringe → cache incompleto
            return "mismatch"
        if not _range_contained(inner, outer):
            return "mismatch"

    # Restrições só no intent (extract sem predicado na coluna) → extract mais amplo → OK
    return "ok"


def _is_stale(
    info: DuckDBTableInfo,
    ttl: int,
    now: datetime | None,
) -> TableGap | None:
    if ttl <= 0:
        return None
    ts = info.materialized_at
    if ts is None:
        return TableGap(
            table_id=info.name,
            reason="stale",
            detail="materialized_at ausente",
        )
    clock = now or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    if (clock - ts).total_seconds() > ttl:
        return TableGap(
            table_id=info.name,
            reason="stale",
            detail=f"ttl={ttl}s excedido",
        )
    return None


def evaluate_sufficiency(
    intent: IntentPlan,
    shard_routing: ShardRouting,
    catalog: DuckDBCatalog,
    config: AgentConfig,
    *,
    dialect: str | None,
    now: datetime | None = None,
) -> SufficiencyDecision:
    """Decide reuse/refresh/unknown de forma determinística (fail-closed)."""
    table_ids = intent_table_ids(intent)
    if not table_ids:
        if catalog.tables:
            return SufficiencyDecision(action="reuse", reasons=["intent sem tabelas"])
        return SufficiencyDecision(
            action="refresh",
            reasons=["catálogo vazio e intent sem tabelas"],
        )

    if not catalog.tables:
        gaps = [
            TableGap(
                table_id=tid,
                reason="missing_table",
                missing_bindings=[b for b in shard_routing.bindings if b.table_id == tid],
            )
            for tid in sorted(table_ids)
        ]
        return SufficiencyDecision(action="refresh", gaps=gaps, reasons=["catálogo vazio"])

    gaps: list[TableGap] = []
    unknowns: list[str] = []

    for tid in sorted(table_ids):
        info = _catalog_entry(catalog, tid, config)
        if info is None:
            gaps.append(
                TableGap(
                    table_id=tid,
                    reason="missing_table",
                    missing_bindings=[b for b in shard_routing.bindings if b.table_id == tid],
                )
            )
            continue

        table = config.try_get_table(tid)
        if table is not None and table.is_sharded:
            required = [b for b in shard_routing.bindings if b.table_id == tid]
            missing = _missing_shard_bindings(required, info.shard_bindings)
            if missing:
                gaps.append(
                    TableGap(
                        table_id=tid,
                        reason="missing_shard",
                        missing_bindings=missing,
                        detail=(
                            f"faltam discriminadores {[b.discriminator_value for b in missing]}"
                        ),
                    )
                )
                continue

        needed = _intent_columns_for_table(intent, tid)
        col_result = _column_coverage(info.source_queries, needed, dialect)
        if col_result == "unknown":
            unknowns.append(f"{tid}: projeção não mapeável")
            continue
        if isinstance(col_result, tuple):
            gaps.append(
                TableGap(
                    table_id=tid,
                    reason="missing_columns",
                    missing_columns=col_result[1],
                )
            )
            continue

        pred = _predicates_cover(intent, tid, info.source_queries, dialect)
        if pred == "unknown":
            unknowns.append(f"{tid}: predicado não decidível")
            continue
        if pred == "mismatch":
            gaps.append(
                TableGap(
                    table_id=tid,
                    reason="predicate_mismatch",
                    detail="extract mais restrito que o intent (G ⇏ F)",
                )
            )
            continue

        stale = _is_stale(info, config.reuse_ttl_seconds, now)
        if stale is not None:
            stale.table_id = tid
            gaps.append(stale)
            continue

    if gaps:
        return SufficiencyDecision(
            action="refresh",
            gaps=gaps,
            reasons=[g.detail or g.reason for g in gaps],
        )
    if unknowns:
        return SufficiencyDecision(action="unknown", reasons=unknowns)
    return SufficiencyDecision(action="reuse")


def build_deterministic_mat_plan(
    decision: SufficiencyDecision,
    catalog: DuckDBCatalog,
    config: AgentConfig,
) -> MaterializationPlan | None:
    """Plano sem LLM para gaps só missing_shard/missing_table em tabelas shardadas.

    ``shard_bindings`` do step = união catálogo ∪ missing (replace/fan-in completo).
    """
    if decision.action != "refresh" or not decision.gaps:
        return None
    allowed = {"missing_shard", "missing_table"}
    if any(g.reason not in allowed for g in decision.gaps):
        return None

    steps: list[MaterializationStep] = []
    for g in decision.gaps:
        table = config.try_get_table(g.table_id)
        if table is None or not table.is_sharded:
            return None
        cached = _catalog_entry(catalog, g.table_id, config)
        existing = list(cached.shard_bindings) if cached else []
        merged: list[ShardBinding] = []
        seen: set[tuple[str, str]] = set()
        for b in existing + list(g.missing_bindings):
            key = _binding_key(b)
            if key in seen:
                continue
            seen.add(key)
            merged.append(b)
        if not merged:
            return None
        steps.append(
            MaterializationStep(
                source_query="",
                target_table=table.id,
                mode="replace",
                shard_bindings=merged,
            )
        )
    return MaterializationPlan(
        steps=steps,
        rationale="deterministic: missing_shard/missing_table",
    )


__all__ = [
    "SufficiencyDecision",
    "TableGap",
    "build_deterministic_mat_plan",
    "evaluate_sufficiency",
    "intent_table_ids",
]
