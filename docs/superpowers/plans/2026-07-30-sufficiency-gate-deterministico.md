# Sufficiency Gate determinístico — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decidir reuse/refresh do DuckDBCatalog de forma determinística (AST + shards + TTL), com LLM só como fallback `unknown`, e um critério único em `sufficiency_gate` e `check_materialization`.

**Architecture:** Função pura `evaluate_sufficiency` em `txt2sql/sufficiency.py`; cascata barato→caro com curto-circuito em gap; `build_deterministic_mat_plan` para refresh só-shard sem LLM; grafo preserva topologia e injeta `SufficiencyDecision` no state.

**Tech Stack:** Python, Pydantic v2, sqlglot, LangGraph, pytest.

**Spec:** `docs/superpowers/specs/2026-07-30-sufficiency-gate-deterministico-design.md`

---

## Arquivos

| Arquivo | Responsabilidade |
|---------|------------------|
| `txt2sql/sufficiency.py` | **Criar.** Modelos `TableGap`/`SufficiencyDecision`, `evaluate_sufficiency`, helpers AST/TTL, `build_deterministic_mat_plan`. |
| `txt2sql/config.py` | Campo `reuse_ttl_seconds` (+ parse YAML `analytics.reuse_ttl_seconds`). |
| `txt2sql/graph.py` | Wire gate / plan_mat / check_mat; campo `sufficiency_decision` no `GraphState`; reexport/`__all__` se útil. |
| `tests/test_sufficiency.py` | **Criar.** Testes unitários da função pura + plano determinístico. |
| `tests/test_graph_dual_path.py` | Gate/check sem LLM quando determinístico; fallback `unknown`. |
| `tests/test_config.py` (ou existente de config) | Default/override TTL. |

**Dependência:** spec de artefatos tipados no `GraphState` já aplicada — gravar `SufficiencyDecision` como instância Pydantic (não `model_dump`).

---

### Task 1: Config — `reuse_ttl_seconds`

**Files:**
- Modify: `txt2sql/config.py`
- Test: `tests/test_config_ttl.py` (criar) ou estender teste de config existente

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_ttl.py
from pathlib import Path

import yaml

from txt2sql.config import load_config


def test_reuse_ttl_default_1800(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        yaml.dump(
            {
                "databases": [{"id": "db", "connection_string": "sqlite://"}],
                "tables": [{"id": "t", "database": "db", "name": "t"}],
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.reuse_ttl_seconds == 1800


def test_reuse_ttl_from_analytics_yaml(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        yaml.dump(
            {
                "databases": [{"id": "db", "connection_string": "sqlite://"}],
                "tables": [{"id": "t", "database": "db", "name": "t"}],
                "analytics": {"reuse_ttl_seconds": 0},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.reuse_ttl_seconds == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_config_ttl.py -v`  
Expected: FAIL (`reuse_ttl_seconds` inexistente)

- [ ] **Step 3: Implement**

Em `AgentConfig`, adicionar:

```python
reuse_ttl_seconds: int = 1800
```

Em `load_config`, após `agent_raw`:

```python
analytics_raw: dict[str, Any] = raw.get("analytics") or {}
reuse_ttl_seconds = int(analytics_raw.get("reuse_ttl_seconds", 1800))
```

Passar `reuse_ttl_seconds=reuse_ttl_seconds` no construtor de `AgentConfig`. Documentar no docstring de `AgentConfig`: TTL de reuse do catálogo DuckDB; `0`/negativo desabilita.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_config_ttl.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add txt2sql/config.py tests/test_config_ttl.py
git commit -m "$(cat <<'EOF'
feat(config): add analytics.reuse_ttl_seconds (default 1800)

EOF
)"
```

---

### Task 2: Modelos + catálogo vazio / cobertura de tabela

**Files:**
- Create: `txt2sql/sufficiency.py`
- Test: `tests/test_sufficiency.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sufficiency.py
from datetime import UTC, datetime

from txt2sql.artifacts import DuckDBCatalog, DuckDBTableInfo, ShardBinding, ShardRouting
from txt2sql.config import AgentConfig, DatabaseConfig, TableConfig
from txt2sql.intent import FilterClause, IntentPlan, MetricClause
from txt2sql.sufficiency import evaluate_sufficiency


def _cfg(*tables: TableConfig) -> AgentConfig:
    return AgentConfig(
        databases=[DatabaseConfig(id="db", connection_string="sqlite://")],
        tables=list(tables),
        dialect="postgres",
    )


def _table(tid: str, *, sharded: bool = False) -> TableConfig:
    from txt2sql.config import ShardingConfig

    return TableConfig(
        id=tid,
        database="db",
        name=tid,
        sharding=(
            ShardingConfig(discriminator_column="filial", resolver="x:y")
            if sharded
            else None
        ),
    )


def test_empty_catalog_refresh() -> None:
    intent = IntentPlan(
        filters=[FilterClause(table_id="vendas", column_id="uf", op="eq", value="SP")]
    )
    d = evaluate_sufficiency(
        intent,
        ShardRouting(),
        DuckDBCatalog(),
        _cfg(_table("vendas")),
        dialect="postgres",
    )
    assert d.action == "refresh"
    assert any(g.reason == "missing_table" for g in d.gaps)


def test_missing_table_gap() -> None:
    intent = IntentPlan(metrics=[MetricClause(table_id="vendas", column_id="valor")])
    catalog = DuckDBCatalog(tables=[DuckDBTableInfo(name="clientes", row_count=1)])
    d = evaluate_sufficiency(
        intent, ShardRouting(), catalog, _cfg(_table("vendas"), _table("clientes")),
        dialect="postgres",
    )
    assert d.action == "refresh"
    assert d.gaps[0].reason == "missing_table"
    assert d.gaps[0].table_id == "vendas"


def test_empty_intent_tables_reuse_if_catalog_nonempty() -> None:
    catalog = DuckDBCatalog(tables=[DuckDBTableInfo(name="vendas", row_count=1)])
    d = evaluate_sufficiency(
        IntentPlan(), ShardRouting(), catalog, _cfg(_table("vendas")), dialect="postgres",
    )
    assert d.action == "reuse"
```

- [ ] **Step 2: Run — expect FAIL (module missing)**

Run: `.venv/bin/pytest tests/test_sufficiency.py::test_empty_catalog_refresh -v`

- [ ] **Step 3: Minimal implementation**

```python
# txt2sql/sufficiency.py
"""Sufficiency gate determinístico: cobre IntentPlan + shards + AST + TTL."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from txt2sql.artifacts import (
    DuckDBCatalog,
    DuckDBTableInfo,
    MaterializationPlan,
    MaterializationStep,
    ShardBinding,
    ShardRouting,
)
from txt2sql.config import AgentConfig
from txt2sql.intent import IntentPlan


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


def evaluate_sufficiency(
    intent: IntentPlan,
    shard_routing: ShardRouting,
    catalog: DuckDBCatalog,
    config: AgentConfig,
    *,
    dialect: str | None,
    now: datetime | None = None,
) -> SufficiencyDecision:
    table_ids = intent_table_ids(intent)
    if not table_ids:
        if catalog.tables:
            return SufficiencyDecision(action="reuse", reasons=["intent sem tabelas"])
        return SufficiencyDecision(
            action="refresh",
            gaps=[],
            reasons=["catálogo vazio e intent sem tabelas"],
        )
    if not catalog.tables:
        gaps = [
            TableGap(
                table_id=tid,
                reason="missing_table",
                missing_bindings=[
                    b for b in shard_routing.bindings if b.table_id == tid
                ],
            )
            for tid in sorted(table_ids)
        ]
        return SufficiencyDecision(
            action="refresh", gaps=gaps, reasons=["catálogo vazio"]
        )

    gaps: list[TableGap] = []
    unknowns: list[str] = []
    for tid in sorted(table_ids):
        info = _catalog_entry(catalog, tid, config)
        if info is None:
            gaps.append(
                TableGap(
                    table_id=tid,
                    reason="missing_table",
                    missing_bindings=[
                        b for b in shard_routing.bindings if b.table_id == tid
                    ],
                )
            )
            continue
        # Tasks 3–5 preenchem shard/colunas/predicados/TTL aqui
        _ = (info, dialect, now, unknowns)  # placeholder até tasks seguintes

    if gaps:
        return SufficiencyDecision(
            action="refresh",
            gaps=gaps,
            reasons=[g.detail or g.reason for g in gaps],
        )
    if unknowns:
        return SufficiencyDecision(action="unknown", reasons=unknowns)
    return SufficiencyDecision(action="reuse")
```

- [ ] **Step 4: Tests pass for Task 2 cases**

Run: `.venv/bin/pytest tests/test_sufficiency.py -v -k "empty_catalog or missing_table or empty_intent"`

- [ ] **Step 5: Commit**

```bash
git add txt2sql/sufficiency.py tests/test_sufficiency.py
git commit -m "$(cat <<'EOF'
feat(sufficiency): add models and table-coverage checks

EOF
)"
```

---

### Task 3: Cobertura de shards

**Files:**
- Modify: `txt2sql/sufficiency.py`
- Test: `tests/test_sufficiency.py`

- [ ] **Step 1: Failing tests**

```python
def _binding(tid: str, disc: str, db: str = "db") -> ShardBinding:
    return ShardBinding(
        table_id=tid,
        discriminator_value=disc,
        database_id=db,
        physical_table=f"{tid}_{disc}",
    )


def test_shard_same_bindings_reuse() -> None:
    b = _binding("vendas", "654")
    intent = IntentPlan(
        filters=[FilterClause(table_id="vendas", column_id="filial", op="eq", value="654")]
    )
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                row_count=10,
                source_queries=["fan-in:1 bindings"],
                shard_bindings=[b],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    routing = ShardRouting(mode="single", bindings=[b], logical_table="vendas")
    # TTL off para isolar shard
    cfg = _cfg(_table("vendas", sharded=True))
    cfg.reuse_ttl_seconds = 0
    d = evaluate_sufficiency(intent, routing, catalog, cfg, dialect="postgres")
    assert d.action == "reuse"


def test_shard_missing_binding_gap() -> None:
    b654 = _binding("vendas", "654")
    b747 = _binding("vendas", "747")
    intent = IntentPlan(
        filters=[
            FilterClause(
                table_id="vendas", column_id="filial", op="in", value=["654", "747"]
            )
        ]
    )
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                row_count=10,
                source_queries=["fan-in:1 bindings"],
                shard_bindings=[b654],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    routing = ShardRouting(mode="multi", bindings=[b654, b747], logical_table="vendas")
    cfg = _cfg(_table("vendas", sharded=True))
    cfg.reuse_ttl_seconds = 0
    d = evaluate_sufficiency(intent, routing, catalog, cfg, dialect="postgres")
    assert d.action == "refresh"
    assert d.gaps[0].reason == "missing_shard"
    assert [b.discriminator_value for b in d.gaps[0].missing_bindings] == ["747"]
```

Nota: `AgentConfig` é dataclass mutável — `cfg.reuse_ttl_seconds = 0` funciona após Task 1. Se frozen no futuro, passar via construtor.

- [ ] **Step 2: Implement shard check** (dentro do loop, após achar `info`):

```python
def _binding_key(b: ShardBinding) -> tuple[str, str]:
    return (b.database_id, b.discriminator_value)


def _missing_shard_bindings(
    required: list[ShardBinding],
    cached: list[ShardBinding],
) -> list[ShardBinding]:
    have = {_binding_key(b) for b in cached}
    return [b for b in required if _binding_key(b) not in have]
```

No loop de `evaluate_sufficiency`, para tabela shardada (`config.try_get_table(tid)` com `is_sharded`):

```python
required = [b for b in shard_routing.bindings if b.table_id == tid]
missing = _missing_shard_bindings(required, info.shard_bindings)
if missing:
    gaps.append(
        TableGap(
            table_id=tid,
            reason="missing_shard",
            missing_bindings=missing,
            detail=f"faltam discriminadores {[b.discriminator_value for b in missing]}",
        )
    )
    continue
```

- [ ] **Step 3: Tests pass**

Run: `.venv/bin/pytest tests/test_sufficiency.py -v -k shard`

- [ ] **Step 4: Commit**

```bash
git add txt2sql/sufficiency.py tests/test_sufficiency.py
git commit -m "$(cat <<'EOF'
feat(sufficiency): check shard binding coverage

EOF
)"
```

---

### Task 4: Cobertura de colunas via AST

**Files:**
- Modify: `txt2sql/sufficiency.py`
- Test: `tests/test_sufficiency.py`

- [ ] **Step 1: Failing tests**

```python
def test_columns_select_star_covers() -> None:
    intent = IntentPlan(
        metrics=[MetricClause(table_id="vendas", column_id="valor", agg="sum")]
    )
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT * FROM vendas"],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    cfg = _cfg(_table("vendas"))
    cfg.reuse_ttl_seconds = 0
    d = evaluate_sufficiency(intent, ShardRouting(), catalog, cfg, dialect="postgres")
    assert d.action == "reuse"


def test_columns_projection_missing() -> None:
    intent = IntentPlan(
        metrics=[MetricClause(table_id="vendas", column_id="c", agg="sum")]
    )
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT a, b FROM vendas"],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    cfg = _cfg(_table("vendas"))
    cfg.reuse_ttl_seconds = 0
    d = evaluate_sufficiency(intent, ShardRouting(), catalog, cfg, dialect="postgres")
    assert d.action == "refresh"
    assert d.gaps[0].reason == "missing_columns"
    assert "c" in d.gaps[0].missing_columns


def test_unparseable_sql_unknown() -> None:
    intent = IntentPlan(
        metrics=[MetricClause(table_id="vendas", column_id="a")]
    )
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["NOT VALID SQL [[["],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    cfg = _cfg(_table("vendas"))
    cfg.reuse_ttl_seconds = 0
    d = evaluate_sufficiency(intent, ShardRouting(), catalog, cfg, dialect="postgres")
    assert d.action == "unknown"
```

- [ ] **Step 2: Implement helpers**

```python
import re

import sqlglot
from sqlglot import exp

_FAN_IN_RE = re.compile(r"^fan-in:\d+ bindings$", re.I)


def _is_synthetic_or_star(sql: str, dialect: str | None) -> bool | None:
    """True=cobre tudo; False=projeção finita; None=unknown (parse fail / expr)."""
    s = sql.strip()
    if _FAN_IN_RE.match(s):
        return True
    try:
        parsed = sqlglot.parse_one(s, dialect=dialect)
    except Exception:  # noqa: BLE001
        return None
    selects = list(parsed.find_all(exp.Select))
    if not selects:
        return None
    # Usa o SELECT raiz
    root = parsed if isinstance(parsed, exp.Select) else selects[0]
    for proj in root.expressions:
        if isinstance(proj, exp.Star):
            return True
        if isinstance(proj, exp.Alias) and isinstance(proj.this, exp.Star):
            return True
    return False  # projeção explícita — caller extrai nomes


def _projected_columns(sql: str, dialect: str | None) -> set[str] | None:
    """None = unknown; set vazio + star tratado antes."""
    flag = _is_synthetic_or_star(sql, dialect)
    if flag is None:
        return None
    if flag is True:
        return set()  # sentinel: caller trata como "all"
    try:
        parsed = sqlglot.parse_one(sql.strip(), dialect=dialect)
    except Exception:  # noqa: BLE001
        return None
    root = parsed if isinstance(parsed, exp.Select) else next(parsed.find_all(exp.Select))
    cols: set[str] = set()
    for proj in root.expressions:
        # alias output name
        if isinstance(proj, exp.Alias):
            cols.add(proj.alias_or_name.lower())
            continue
        if isinstance(proj, exp.Column):
            cols.add(proj.name.lower())
            continue
        # expressão sem alias mapeável
        return None
    return cols


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
```

No loop, após shards OK:

```python
needed = _intent_columns_for_table(intent, tid)
if needed:
    covers_all = False
    covered: set[str] = set()
    col_unknown = False
    for q in info.source_queries or []:
        flag = _is_synthetic_or_star(q, dialect)
        if flag is None:
            col_unknown = True
            break
        if flag is True:
            covers_all = True
            break
        proj = _projected_columns(q, dialect)
        if proj is None:
            col_unknown = True
            break
        covered |= proj
    if col_unknown:
        unknowns.append(f"{tid}: projeção não mapeável")
        continue
    if not covers_all:
        missing_cols = sorted(needed - covered)
        if missing_cols:
            gaps.append(
                TableGap(
                    table_id=tid,
                    reason="missing_columns",
                    missing_columns=missing_cols,
                )
            )
            continue
```

Convenção: `covers_all` (star/fan-in) → não gera gap de colunas. União de projeções entre múltiplas queries.

- [ ] **Step 3: Tests pass + commit**

```bash
git add txt2sql/sufficiency.py tests/test_sufficiency.py
git commit -m "$(cat <<'EOF'
feat(sufficiency): AST column coverage from source_queries

EOF
)"
```

---

### Task 5: Subsunção de predicados

**Files:**
- Modify: `txt2sql/sufficiency.py`
- Test: `tests/test_sufficiency.py`

- [ ] **Step 1: Failing tests**

```python
def test_predicate_eq_reuse_and_mismatch() -> None:
    cfg = _cfg(_table("vendas"))
    cfg.reuse_ttl_seconds = 0
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT * FROM vendas WHERE uf = 'SP'"],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    ok = evaluate_sufficiency(
        IntentPlan(
            filters=[FilterClause(table_id="vendas", column_id="uf", op="eq", value="SP")]
        ),
        ShardRouting(),
        catalog,
        cfg,
        dialect="postgres",
    )
    assert ok.action == "reuse"
    bad = evaluate_sufficiency(
        IntentPlan(
            filters=[FilterClause(table_id="vendas", column_id="uf", op="eq", value="RJ")]
        ),
        ShardRouting(),
        catalog,
        cfg,
        dialect="postgres",
    )
    assert bad.action == "refresh"
    assert bad.gaps[0].reason == "predicate_mismatch"


def test_predicate_range_contained() -> None:
    cfg = _cfg(_table("vendas"))
    cfg.reuse_ttl_seconds = 0
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT * FROM vendas WHERE valor > 100"],
                materialized_at=datetime.now(UTC),
            )
        ]
    )
    d = evaluate_sufficiency(
        IntentPlan(
            filters=[FilterClause(table_id="vendas", column_id="valor", op="gt", value=500)]
        ),
        ShardRouting(),
        catalog,
        cfg,
        dialect="postgres",
    )
    assert d.action == "reuse"


def test_predicate_or_like_unknown() -> None:
    cfg = _cfg(_table("vendas"))
    cfg.reuse_ttl_seconds = 0
    for sql in (
        "SELECT * FROM vendas WHERE uf = 'SP' OR uf = 'RJ'",
        "SELECT * FROM vendas WHERE nome LIKE '%a%'",
    ):
        catalog = DuckDBCatalog(
            tables=[
                DuckDBTableInfo(
                    name="vendas",
                    source_queries=[sql],
                    materialized_at=datetime.now(UTC),
                )
            ]
        )
        d = evaluate_sufficiency(
            IntentPlan(
                filters=[FilterClause(table_id="vendas", column_id="uf", op="eq", value="SP")]
            ),
            ShardRouting(),
            catalog,
            cfg,
            dialect="postgres",
        )
        assert d.action == "unknown", sql
```

- [ ] **Step 2: Implement conservative subsumption**

Estratégia mínima (fail-closed → `unknown`/`refresh`):

1. Se todas as `source_queries` são sintéticas/star **sem WHERE** → predicados OK.
2. Se `len(source_queries) > 1` e alguma não-sintética → `unknown`.
3. Extrair predicados do WHERE do extract como mapa `col → Constraint` (eq set, in set, range lo/hi). `OR`/`LIKE`/função → `unknown`.
4. Para cada filtro do intent na tabela (ops suportados: `eq`,`in`,`gt`,`gte`,`lt`,`lte`,`between`):
   - Se extract não tem predicado naquela coluna → OK (extract mais amplo).
   - Senão verificar `G ⇒ F` (valores intent ⊆ extract; range intent ⊆ range extract).
5. Ops intent `like`/`ne`/`is_null` → `unknown`.
6. Coluna no extract com predicado que o intent **não** restringe (ou restringe de forma não contida) → `predicate_mismatch` se claramente incompatível; se não der para decidir → `unknown`.

Helper esboço:

```python
@dataclass
class _ColConstraint:
    eq_values: set[str] | None = None  # inclusive set (eq/in)
    lower: tuple[float | str, bool] | None = None  # (bound, inclusive)
    upper: tuple[float | str, bool] | None = None


def _predicates_cover(
    intent: IntentPlan,
    table_id: str,
    source_queries: list[str],
    dialect: str | None,
) -> Literal["ok", "mismatch", "unknown"]:
    ...
```

Mapear literais sqlglot → str/número de forma estável (`str(literal.this)`).

- [ ] **Step 3: Tests pass + commit**

```bash
git add txt2sql/sufficiency.py tests/test_sufficiency.py
git commit -m "$(cat <<'EOF'
feat(sufficiency): conservative predicate subsumption

EOF
)"
```

---

### Task 6: TTL / frescor

**Files:**
- Modify: `txt2sql/sufficiency.py`
- Test: `tests/test_sufficiency.py`

- [ ] **Step 1: Failing tests**

```python
def test_ttl_stale_and_missing_timestamp() -> None:
    cfg = _cfg(_table("vendas"))
    # default 1800
    old = datetime(2020, 1, 1, tzinfo=UTC)
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT * FROM vendas"],
                materialized_at=old,
            )
        ]
    )
    intent = IntentPlan(metrics=[MetricClause(table_id="vendas", column_id="a")])
    d = evaluate_sufficiency(
        intent, ShardRouting(), catalog, cfg, dialect="postgres",
        now=datetime(2020, 1, 1, 1, 0, tzinfo=UTC),  # 1h depois
    )
    assert d.action == "refresh"
    assert d.gaps[0].reason == "stale"

    catalog2 = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT * FROM vendas"],
                materialized_at=None,
            )
        ]
    )
    d2 = evaluate_sufficiency(
        intent, ShardRouting(), catalog2, cfg, dialect="postgres",
        now=datetime.now(UTC),
    )
    assert d2.gaps[0].reason == "stale"


def test_ttl_disabled_ignores_age() -> None:
    cfg = _cfg(_table("vendas"))
    cfg.reuse_ttl_seconds = 0
    catalog = DuckDBCatalog(
        tables=[
            DuckDBTableInfo(
                name="vendas",
                source_queries=["SELECT * FROM vendas"],
                materialized_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        ]
    )
    d = evaluate_sufficiency(
        IntentPlan(metrics=[MetricClause(table_id="vendas", column_id="a")]),
        ShardRouting(),
        catalog,
        cfg,
        dialect="postgres",
        now=datetime.now(UTC),
    )
    assert d.action == "reuse"
```

- [ ] **Step 2: Implement no fim do loop por tabela**

```python
ttl = config.reuse_ttl_seconds
if ttl is not None and ttl > 0:
    ts = info.materialized_at
    if ts is None:
        gaps.append(TableGap(table_id=tid, reason="stale", detail="materialized_at ausente"))
        continue
    clock = now or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    if (clock - ts).total_seconds() > ttl:
        gaps.append(TableGap(table_id=tid, reason="stale", detail=f"ttl={ttl}s excedido"))
        continue
```

- [ ] **Step 3: Commit**

```bash
git add txt2sql/sufficiency.py tests/test_sufficiency.py
git commit -m "$(cat <<'EOF'
feat(sufficiency): TTL freshness check (default 30min)

EOF
)"
```

---

### Task 7: `build_deterministic_mat_plan`

**Files:**
- Modify: `txt2sql/sufficiency.py`
- Test: `tests/test_sufficiency.py`

- [ ] **Step 1: Failing tests**

```python
from txt2sql.sufficiency import build_deterministic_mat_plan


def test_deterministic_plan_unions_cached_bindings() -> None:
    b654 = _binding("vendas", "654")
    b747 = _binding("vendas", "747")
    decision = SufficiencyDecision(
        action="refresh",
        gaps=[
            TableGap(
                table_id="vendas",
                reason="missing_shard",
                missing_bindings=[b747],
            )
        ],
    )
    catalog = DuckDBCatalog(
        tables=[DuckDBTableInfo(name="vendas", shard_bindings=[b654])]
    )
    plan = build_deterministic_mat_plan(
        decision, catalog, _cfg(_table("vendas", sharded=True))
    )
    assert plan is not None
    assert len(plan.steps) == 1
    assert plan.steps[0].source_query == ""
    discs = {b.discriminator_value for b in plan.steps[0].shard_bindings}
    assert discs == {"654", "747"}


def test_deterministic_plan_rejects_missing_columns() -> None:
    decision = SufficiencyDecision(
        action="refresh",
        gaps=[
            TableGap(table_id="vendas", reason="missing_columns", missing_columns=["c"])
        ],
    )
    assert (
        build_deterministic_mat_plan(
            decision, DuckDBCatalog(), _cfg(_table("vendas", sharded=True))
        )
        is None
    )
```

- [ ] **Step 2: Implement**

```python
def build_deterministic_mat_plan(
    decision: SufficiencyDecision,
    catalog: DuckDBCatalog,
    config: AgentConfig,
) -> MaterializationPlan | None:
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
            return None  # shardada sem bindings → precisa LLM/clarificação
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
```

**Invariante crítico:** nunca passar só `missing_bindings` — `materialize` faz `replace`/`fan-in` completo.

- [ ] **Step 3: Commit**

```bash
git add txt2sql/sufficiency.py tests/test_sufficiency.py
git commit -m "$(cat <<'EOF'
feat(sufficiency): deterministic MaterializationPlan for shard gaps

EOF
)"
```

---

### Task 8: Wire `sufficiency_gate` + state

**Files:**
- Modify: `txt2sql/graph.py`
- Test: `tests/test_graph_dual_path.py`

- [ ] **Step 1: Failing graph test (LLM counter)**

Adicionar teste que monta catálogo já cobrindo o intent (tabela + `SELECT *` + `materialized_at` fresco, TTL off ou fresco) no path analítico e usa fake LLM com contador:

```python
def test_sufficiency_gate_skips_llm_when_deterministic(monkeypatch):
    """gate_llm não é chamado quando evaluate_sufficiency = reuse."""
    # Arrange: agent com fake LLM que falha se GateDecision for pedido;
    # pré-popular state via invoke parcial OU monkeypatch evaluate para return reuse
    ...
    assert gate_llm_calls == 0
    assert result.get("gate_action") == "reuse"
    assert result.get("sufficiency_decision").action == "reuse"
```

Padrão prático no repo: monkeypatch `txt2sql.graph.evaluate_sufficiency` (após import) ou injetar catálogo via sessão — seguir estilo de `test_graph_dual_path.py` existente (Fake LLM queue). O essencial: quando a fila **não** tem `GateDecision` e o catálogo cobre, o grafo não deve estourar `StopIteration` no gate.

- [ ] **Step 2: Implement wire**

1. Importar `evaluate_sufficiency`, `SufficiencyDecision`, `build_deterministic_mat_plan`.
2. Em `GraphState`:

```python
sufficiency_decision: SufficiencyDecision | None
```

3. Helper `_sufficiency_decision(state)`.
4. Substituir corpo de `sufficiency_gate`:

```python
def sufficiency_gate(state: GraphState) -> dict[str, Any]:
    catalog = _catalog(state)
    budget = _budget(state)
    if budget.exhausted("gate_visits"):
        return {"gate_action": "refresh", "budget": budget}
    plan = _coerce_intent_plan(state.get("intent_plan"))
    shard = _shard_routing(state)
    decision = evaluate_sufficiency(
        plan, shard, catalog, config, dialect=default_dialect
    )
    if decision.action == "unknown":
        context = (
            "Decida se o DuckDBCatalog cobre o IntentPlan (reuse) ou precisa refresh.\n"
            f"Diagnóstico determinístico:\n{_dump_json(decision.reasons)}\n"
            f"IntentPlan:\n{_dump_json(plan)}\n"
            f"Catalog:\n{_dump_json(catalog)}"
        )
        gate = gate_llm.invoke([SystemMessage(content=context)])
        # coerce GateDecision como hoje
        action = gate.action if isinstance(gate, GateDecision) else ...
        decision = SufficiencyDecision(action=action, reasons=decision.reasons)
    new_budget = budget.model_copy(
        update={"gate_visits": budget.gate_visits + 1}
    )
    return {
        "gate_action": decision.action if decision.action != "unknown" else "refresh",
        # nota: após LLM, action é reuse|refresh; unknown não deve vazar para roteamento
        "sufficiency_decision": decision,
        "budget": new_budget,
    }
```

Se LLM devolver algo inválido → fail-closed `refresh`.

Roteamento existente usa `gate_action` ∈ {reuse, refresh} — manter.

- [ ] **Step 3: Teste de fallback unknown**

Fake: monkeypatch `evaluate_sufficiency` → `SufficiencyDecision(action="unknown", reasons=["or"])` e garantir que `GateDecision` da fila é consumido.

- [ ] **Step 4: Commit**

```bash
git add txt2sql/graph.py tests/test_graph_dual_path.py
git commit -m "$(cat <<'EOF'
feat(graph): deterministic sufficiency_gate with LLM fallback

EOF
)"
```

---

### Task 9: Wire `check_materialization`

**Files:**
- Modify: `txt2sql/graph.py`
- Test: `tests/test_graph_dual_path.py`

- [ ] **Step 1: Replace body** (após checks de budget/last_result):

```python
plan = _coerce_intent_plan(state.get("intent_plan"))
catalog = _catalog(state)
shard = _shard_routing(state)
decision = evaluate_sufficiency(
    plan, shard, catalog, config, dialect=default_dialect
)
if decision.action == "reuse":
    return {
        "mat_ready": True,
        "partial": False,
        "sufficiency_decision": decision,
    }
if decision.action == "refresh":
    return {
        "mat_ready": False,
        "partial": False,
        "sufficiency_decision": decision,
    }
# unknown → fallback LLM (MaterializationCheck) como hoje
context = (
    "Avalie se o DuckDBCatalog cobre o IntentPlan para gerar SQL analítico.\n"
    f"Diagnóstico:\n{_dump_json(decision.reasons)}\n"
    ...
)
```

Remover uso de `_catalog_covers_tables` neste nó (função pode permanecer temporariamente se outros callers; se só aqui, deletar ou delegar a `evaluate_sufficiency`).

- [ ] **Step 2: Teste de coerência**

Mesmo catálogo/intent: gate e check_mat produzem a mesma decisão determinística (reuse→mat_ready True; refresh→False) sem LLM.

- [ ] **Step 3: Commit**

```bash
git add txt2sql/graph.py tests/test_graph_dual_path.py
git commit -m "$(cat <<'EOF'
feat(graph): unify check_materialization with evaluate_sufficiency

EOF
)"
```

---

### Task 10: Wire `plan_materialization`

**Files:**
- Modify: `txt2sql/graph.py`
- Test: `tests/test_sufficiency.py` + ajuste em `test_graph_dual_path.py`

- [ ] **Step 1: Implement**

```python
def plan_materialization(state: GraphState) -> dict[str, Any]:
    plan = _coerce_intent_plan(state.get("intent_plan"))
    shard = _shard_routing(state)
    catalog = _catalog(state)
    decision = state.get("sufficiency_decision")
    if isinstance(decision, dict):
        decision = SufficiencyDecision.model_validate(decision)
    if isinstance(decision, SufficiencyDecision):
        det = build_deterministic_mat_plan(decision, catalog, config)
        if det is not None:
            return {"materialization_plan": det}
    logical_ids = sorted(intent_table_ids(plan)) or [t.id for t in config.tables]
    gaps_blob = ""
    if isinstance(decision, SufficiencyDecision) and decision.gaps:
        gaps_blob = f"Gaps (materializar só o necessário):\n{_dump_json(decision.gaps)}\n"
    context = (
        "Gere MaterializationPlan com extracts filtrados (sem agregação pesada na origem).\n"
        "IMPORTANTE: target_table DEVE ser exatamente um table_id lógico da config "
        f"({logical_ids}). Não invente nomes como 'recebiveis_filtered_…'.\n"
        f"{gaps_blob}"
        f"IntentPlan:\n{_dump_json(plan, indent=2)}\n"
        f"ShardRouting:\n{_dump_json(shard, indent=2)}"
    )
    mat = mat_llm.invoke([SystemMessage(content=context)])
    ...
```

Opcional: trocar `_intent_table_ids` do graph por `intent_table_ids` importado de `sufficiency` (DRY).

- [ ] **Step 2: Integration test** — path analítico com catálogo parcial (só 654) + routing 654+747 → plano determinístico sem consumir `MaterializationPlan` da fila LLM.

- [ ] **Step 3: Commit**

```bash
git add txt2sql/graph.py tests/test_graph_dual_path.py tests/test_sufficiency.py
git commit -m "$(cat <<'EOF'
feat(graph): deterministic plan_materialization for shard gaps

EOF
)"
```

---

### Task 11: Regressão / smoke

- [ ] **Step 1: Run suite**

```bash
.venv/bin/pytest tests/test_sufficiency.py tests/test_config_ttl.py tests/test_graph_dual_path.py tests/test_intent_graph.py -v
.venv/bin/python smoke_test_graph.py
.venv/bin/ruff check txt2sql/sufficiency.py txt2sql/graph.py txt2sql/config.py
```

Expected: PASS (falhas pré-existentes fora do escopo não bloqueiam).

- [ ] **Step 2: Export público interno**

Em `txt2sql/sufficiency.py`:

```python
__all__ = [
    "TableGap",
    "SufficiencyDecision",
    "evaluate_sufficiency",
    "build_deterministic_mat_plan",
    "intent_table_ids",
]
```

Não é obrigatório exportar em `txt2sql/__init__.py` (fora de escopo da API pública).

- [ ] **Step 3: Commit final se houver leftovers**

---

## Gaps fechados neste plano (revisão vs design original)

| Gap | Decisão no plano |
|-----|------------------|
| `shard_bindings` só com missing + `replace=True` apaga cache | **União** catálogo ∪ missing no step determinístico |
| `missing_table` sem bindings | `evaluate_sufficiency` preenche `missing_bindings` a partir de `ShardRouting` |
| `analytics.*` inexistente em `config.py` | Campo `AgentConfig.reuse_ttl_seconds` + parse YAML |
| Queries sintéticas e predicados | fan-in / star sem WHERE → cobrem predicados de domínio |
| Múltiplas `source_queries` com WHERE | `unknown` |
| Colunas de JOIN | Incluir `JoinOn` em `_intent_columns_for_table` |
| Intent sem tabelas | Espelha `_catalog_covers_tables` |
| `gate_visits` só no path LLM | Incrementa sempre |
| `unknown` vazando para `gate_action` | Roteamento só `reuse`\|`refresh`; LLM resolve unknown |
| Agregação gap+unknown | Gap ganha → `refresh` |
| Dependência artefatos tipados | Gravar instância `SufficiencyDecision` no state |

## Fora de escopo (não implementar)

- CDC / invalidação por escrita
- Subsunção com `OR` distribuído
- Plano determinístico para `stale` / `missing_columns` / tabelas não shardadas
- Remoção de `GateDecision` / `MaterializationCheck`
- Docs de produto além do campo YAML (opcional follow-up)

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-30-sufficiency-gate-deterministico.md`. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks
2. **Inline Execution** — execute tasks in this session with executing-plans checkpoints

Which approach?
