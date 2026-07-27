# Multi-shard DuckDB fan-in Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permitir análise cross-shard no DuckDB via tool `materialize_sharded_table` (lista conhecida de discriminadores, limite configurável, tabela lógica única).

**Architecture:** Estender `DuckDBSession.materialize` com `append`/`replace`. Nova função de orquestração em `txt2sql/db/multi_shard.py` (truncate → resolve → agrupa → materializa com `filter_sql`). O grafo ganha nó/tool dedicado; `check_query` reconhece nome lógico em `multi_materialized` e força DuckDB. Caminho single permanece intacto.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.x, DuckDB, LangGraph, pytest.

---

## File map

| File | Responsibility |
| --- | --- |
| `txt2sql/db/duckdb_layer.py` | `append` / `replace` em `materialize`; `drop_logical` |
| `txt2sql/db/multi_shard.py` | Orquestração fan-in + builders de filtro/agrupamento |
| `txt2sql/config.py` | `max_shard_discriminators` em `AgentConfig` + `load_config` |
| `txt2sql/agent.py` | Estado, tool, nó, roteamento, guardrail lógico |
| `txt2sql/prompts.py` | Protocolo multi |
| `tests/test_duckdb_layer.py` | Append / replace |
| `tests/test_multi_shard.py` | Orquestração, limite, rejeições |
| `docs/adr/0002-…` | Consequência fan-in |
| `docs/referencia/api.md` | Tool + config |
| `examples/recebiveis.yaml` | Exemplo `max_shard_discriminators` |

---

### Task 1: DuckDB append/replace

**Files:**
- Modify: `txt2sql/db/duckdb_layer.py`
- Modify: `tests/test_duckdb_layer.py`

- [ ] **Step 1: Testes falhando — append e replace**

Acrescentar em `tests/test_duckdb_layer.py`:

```python
def _source_engine_filtered():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE origem (cnpj TEXT, valor REAL)"))
        conn.execute(
            text("INSERT INTO origem (cnpj, valor) VALUES (:c, :v)"),
            [
                {"c": "111", "v": 10.0},
                {"c": "222", "v": 20.0},
                {"c": "111", "v": 5.0},
            ],
        )
    return engine


def test_materialize_append_merges_sources() -> None:
    eng_a = create_engine("sqlite:///:memory:")
    eng_b = create_engine("sqlite:///:memory:")
    with eng_a.begin() as c:
        c.execute(text("CREATE TABLE t_a (cnpj TEXT, valor REAL)"))
        c.execute(text("INSERT INTO t_a VALUES ('111', 10.0)"))
    with eng_b.begin() as c:
        c.execute(text("CREATE TABLE t_b (cnpj TEXT, valor REAL)"))
        c.execute(text("INSERT INTO t_b VALUES ('222', 20.0)"))
    cfg = _table()
    session = DuckDBSession()
    try:
        session.materialize(cfg, eng_a, physical_name="t_a", replace=True)
        session.materialize(cfg, eng_b, physical_name="t_b", append=True)
        rows = session.execute(
            "SELECT cnpj, SUM(valor) AS s FROM origem_logica GROUP BY cnpj ORDER BY cnpj"
        )
        assert [(r["cnpj"], r["s"]) for r in rows] == [("111", 10.0), ("222", 20.0)]
    finally:
        session.close()


def test_materialize_replace_clears_previous() -> None:
    eng = _source_engine_with_rows(3)
    session = DuckDBSession()
    try:
        cfg = _table()
        session.materialize(cfg, eng, physical_name="origem")
        session.materialize(cfg, eng, physical_name="origem", replace=True)
        rows = session.execute("SELECT COUNT(*) AS c FROM origem_logica")
        assert rows[0]["c"] == 3
    finally:
        session.close()


def test_materialize_filter_sql() -> None:
    eng = _source_engine_filtered()
    session = DuckDBSession()
    try:
        session.materialize(
            _table(), eng, physical_name="origem", filter_sql="cnpj IN ('111')"
        )
        rows = session.execute("SELECT COUNT(*) AS c FROM origem_logica")
        assert rows[0]["c"] == 2
    finally:
        session.close()
```

- [ ] **Step 2: Rodar e confirmar falha**

```bash
.venv/bin/pytest tests/test_duckdb_layer.py::test_materialize_append_merges_sources -v
```

Expected: FAIL (append/replace inexistentes)

- [ ] **Step 3: Implementar `replace`/`append` em `materialize`**

Em `DuckDBSession.materialize`, adicionar kwargs `append: bool = False`, `replace: bool = False`:

- `replace=True`: `DROP TABLE IF EXISTS "{logical}"`, remover de `_materialized`, seguir como create.
- Se `logical` em `_materialized` e `append=False` e `replace=False`: return (idempotência atual).
- Se `append=True` e já materializada: só `SELECT`+insert (sem CREATE); se vazia, noop de insert.
- Se `append=True` e ainda não materializada: criar como fluxo normal.
- `append` e `replace` juntos → `ValueError`.

Manter streaming em lotes.

- [ ] **Step 4: Rodar testes DuckDB**

```bash
.venv/bin/pytest tests/test_duckdb_layer.py -v
```

Expected: PASS (incluindo idempotência antiga)

- [ ] **Step 5: Commit**

```bash
git add tests/test_duckdb_layer.py txt2sql/db/duckdb_layer.py
git commit -m "feat(duckdb): support append/replace materialize for multi-shard"
```

---

### Task 2: Config `max_shard_discriminators`

**Files:**
- Modify: `txt2sql/config.py`
- Create/Modify: `tests/test_multi_shard.py` (começa com parse)

- [ ] **Step 1: Teste de parse**

```python
"""Testes do fan-in multi-shard."""

from __future__ import annotations

from pathlib import Path

import yaml

from txt2sql.config import load_config


def test_load_config_max_shard_discriminators(tmp_path: Path) -> None:
    raw = {
        "databases": [{"id": "db", "connection_string": "sqlite:///:memory:"}],
        "tables": [{"id": "t", "database": "db", "name": "t"}],
        "agent": {"max_shard_discriminators": 7},
    }
    p = tmp_path / "c.yaml"
    p.write_text(yaml.dump(raw), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.max_shard_discriminators == 7


def test_load_config_max_shard_discriminators_default(tmp_path: Path) -> None:
    raw = {
        "databases": [{"id": "db", "connection_string": "sqlite:///:memory:"}],
        "tables": [{"id": "t", "database": "db", "name": "t"}],
    }
    p = tmp_path / "c.yaml"
    p.write_text(yaml.dump(raw), encoding="utf-8")
    assert load_config(p).max_shard_discriminators == 20
```

- [ ] **Step 2: Rodar — falha**

```bash
.venv/bin/pytest tests/test_multi_shard.py::test_load_config_max_shard_discriminators -v
```

- [ ] **Step 3: Implementar campo**

Em `AgentConfig`:
```python
max_shard_discriminators: int = 20
```
Docstring: máximo de discriminadores por chamada multi-shard.

Em `load_config`:
```python
max_shard_discriminators=int(agent_raw.get("max_shard_discriminators", 20)),
```

Validar `>= 1` em `_validate` ou `__post_init__` path de agent (se `< 1` → `ValueError`).

- [ ] **Step 4: Pass + commit**

```bash
.venv/bin/pytest tests/test_multi_shard.py::test_load_config_max_shard_discriminators tests/test_multi_shard.py::test_load_config_max_shard_discriminators_default -v
git add txt2sql/config.py tests/test_multi_shard.py
git commit -m "feat(config): add max_shard_discriminators"
```

---

### Task 3: Módulo `multi_shard` (orquestração)

**Files:**
- Create: `txt2sql/db/multi_shard.py`
- Modify: `tests/test_multi_shard.py`
- Modify: `txt2sql/db/__init__.py` se exportar (só se já exportar peers)

- [ ] **Step 1: Testes da orquestração**

Acrescentar helpers e testes em `tests/test_multi_shard.py` usando engines SQLite + `ShardResolver` fake via `TableConfig`/`ShardingConfig` com resolver local no próprio arquivo de teste:

```python
from txt2sql.config import (
    AgentConfig,
    DatabaseConfig,
    DuckDBConfig,
    ShardResult,
    ShardingConfig,
    TableConfig,
)
from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.db.multi_shard import (
    MultiMaterializeResult,
    build_in_filter,
    materialize_sharded_values,
)
from txt2sql.db.registry import DatabaseRegistry
from txt2sql.db.shard import ShardResolver


def _resolver_a(v: str) -> ShardResult:
    if v.startswith("1"):
        return ShardResult(database_id="db_a", table_name="rec_a")
    return ShardResult(database_id="db_b", table_name="rec_b")


# registrar no módulo de teste para dotted path:
# tests.test_multi_shard:_resolver_a  — ou injetar via monkeypatch no ShardResolver._resolvers


def test_build_in_filter_escapes_quotes() -> None:
    assert build_in_filter("cnpj", ["a'b", "c"]) == "cnpj IN ('a''b', 'c')"


def test_materialize_sharded_values_groups_and_filters(tmp_path) -> None:
    # dois engines, dados por shard; max alto; 3 valores (2 no mesmo shard)
    ...
    result = materialize_sharded_values(
        table=table,
        values=["111", "122", "222"],
        max_discriminators=20,
        resolver=resolver,
        registry=registry,
        session=session,
    )
    assert result.truncated is False
    assert set(result.materialized_values) == {"111", "122", "222"}
    rows = session.execute(
        "SELECT cnpj, SUM(valor) AS s FROM recebiveis GROUP BY cnpj ORDER BY cnpj"
    )
    ...


def test_materialize_sharded_values_truncates() -> None:
    ...
    assert result.truncated is True
    assert result.omitted_count == 1
    assert len(result.materialized_values) == 2


def test_materialize_sharded_values_rejects_empty() -> None:
    with pytest.raises(ValueError, match="vazio"):
        materialize_sharded_values(..., values=[], ...)


def test_materialize_sharded_values_rejects_single() -> None:
    with pytest.raises(ValueError, match="resolve_shard"):
        materialize_sharded_values(..., values=["111"], ...)
```

Implementar o corpo dos testes com engines reais (como no Task 1). Para o resolver, setar `resolver._resolvers[table.id] = _resolver_a` após construir `ShardResolver`, **ou** usar dotted path `tests.test_multi_shard:_resolver_a` se o pacote testes for importável — preferir monkeypatch no `_resolvers` para simplicidade.

- [ ] **Step 2: Rodar — falha (módulo ausente)**

```bash
.venv/bin/pytest tests/test_multi_shard.py -v
```

- [ ] **Step 3: Implementar `txt2sql/db/multi_shard.py`**

```python
"""Fan-in multi-shard: materializa vários discriminadores numa tabela DuckDB lógica."""

from __future__ import annotations

from dataclasses import dataclass

from txt2sql.config import TableConfig
from txt2sql.db.duckdb_layer import DuckDBSession
from txt2sql.db.registry import DatabaseRegistry
from txt2sql.db.shard import ShardResolver


@dataclass(frozen=True)
class MultiMaterializeResult:
    table_id: str
    materialized_values: list[str]
    truncated: bool
    omitted_count: int
    message: str

    def to_dict(self) -> dict:
        return {
            "table_id": self.table_id,
            "materialized_values": self.materialized_values,
            "truncated": self.truncated,
            "omitted_count": self.omitted_count,
            "message": self.message,
        }


def build_in_filter(column: str, values: list[str]) -> str:
    literals = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return f"{column} IN ({literals})"


def materialize_sharded_values(
    *,
    table: TableConfig,
    values: list[str],
    max_discriminators: int,
    resolver: ShardResolver,
    registry: DatabaseRegistry,
    session: DuckDBSession,
) -> MultiMaterializeResult:
    if not table.is_sharded or not table.uses_duckdb:
        raise ValueError(
            f"Tabela {table.id!r} precisa ser shardada e com DuckDB habilitado."
        )
    cleaned = [str(v).strip() for v in values if v is not None and str(v).strip()]
    # dedupe preservando ordem
    seen: set[str] = set()
    unique: list[str] = []
    for v in cleaned:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    if not unique:
        raise ValueError("Lista de discriminadores vazia. Peça os valores ao usuário.")
    if len(unique) == 1:
        raise ValueError(
            "Um único discriminador: use resolve_shard + sql_db_query (caminho single)."
        )

    truncated = len(unique) > max_discriminators
    omitted = max(0, len(unique) - max_discriminators)
    used = unique[:max_discriminators]

    # resolve all (fail-closed)
    groups: dict[tuple[str, str], list[str]] = {}
    for v in used:
        shard = resolver.resolve(table.id, v)
        key = (shard.database_id, shard.table_name)
        groups.setdefault(key, []).append(v)

    disc_col = table.sharding.discriminator_column
    first = True
    for (db_id, physical), group_vals in groups.items():
        engine = registry.get_engine(db_id)
        filt = build_in_filter(disc_col, group_vals)
        session.materialize(
            table_config=table,
            source_engine=engine,
            physical_name=physical,
            filter_sql=filt,
            replace=first,
            append=not first,
        )
        first = False

    if truncated:
        msg = (
            f"Limite max_shard_discriminators={max_discriminators} atingido; "
            f"{omitted} valor(es) omitido(s). Análise parcial com {len(used)} valor(es)."
        )
    else:
        msg = f"Materializados {len(used)} discriminador(es) em {table.id!r}."

    return MultiMaterializeResult(
        table_id=table.id,
        materialized_values=used,
        truncated=truncated,
        omitted_count=omitted,
        message=msg,
    )
```

- [ ] **Step 4: Pass + commit**

```bash
.venv/bin/pytest tests/test_multi_shard.py -v
git add txt2sql/db/multi_shard.py tests/test_multi_shard.py
git commit -m "feat(shard): orchestrate multi-shard DuckDB fan-in"
```

---

### Task 4: Wiring no agente (tool + estado + roteamento)

**Files:**
- Modify: `txt2sql/agent.py`
- Modify: `tests/test_multi_shard.py` (testes de integração leves do nó, se viável) **ou** cobrir via funções extraídas

- [ ] **Step 1: Estender `AgentState`**

```python
multi_materialized: dict[str, dict[str, Any]]  # table_id -> meta
```

Em `init_turn`: `"multi_materialized": {}`.

- [ ] **Step 2: Schema + tool placeholder**

```python
class MaterializeShardedInput(BaseModel):
    table_id: str = Field(...)
    discriminator_values: list[str] = Field(
        description="Lista com 2+ valores do discriminador (ex.: CNPJs)."
    )
```

Incluir `StructuredTool` `materialize_sharded_table` no toolkit quando houver tabelas shardadas+duckdb.

- [ ] **Step 3: Nó `run_materialize_sharded`**

Para cada tool call com esse nome:
- valida tabela
- chama `materialize_sharded_values(...)`
- em erro: ToolMessage com `ERRO: ...`
- em sucesso: JSON `to_dict()` + atualiza `multi_materialized[table_id]` e `resolved_shards` cache para os pares usados
- garante `duckdb_session` existente

- [ ] **Step 4: `route_after_generate`**

Prioridade: `materialize_sharded_table` → `run_materialize_sharded` (antes ou depois de resolve_shard; se ambos no mesmo AI message, preferir processar materialize se presente — ou processar um tipo por vez como hoje). Seguir padrão atual: um tipo por rodada; se `materialize_sharded_table` in names → esse nó.

- [ ] **Step 5: `check_query` + `_resolve_target`**

- `allowed_tables` += ids em `multi_materialized` + nomes lógicos `config.get_table(tid).name` se necessário.
- Se SQL referencia `table_id` (ou name) presente em `multi_materialized`: `use_duckdb=True`, `duck_table_id=tid`, `duck_physical=None`.
- Em `materialize_duckdb`: se `session.is_materialized(table.id)`, pular materialize (já feito pelo tool multi).

- [ ] **Step 6: Prompt wiring já na Task 5; aqui só grafo**

Edges: `run_materialize_sharded` → `generate_query`.

- [ ] **Step 7: Teste de integração mínimo**

Preferir testar `_resolve_target` / allowed via smoke interno: se difícil extrair, testar `materialize_sharded_values` (já coberto) + um teste que monta `AgentConfig` mínimo e invoca só a lógica de allowed tables extraindo helper se necessário.

Alternativa pragmática: estender `smoke_test_graph.py` na Task 6.

- [ ] **Step 8: Commit**

```bash
git add txt2sql/agent.py
git commit -m "feat(agent): wire materialize_sharded_table tool and routing"
```

---

### Task 5: Prompt

**Files:**
- Modify: `txt2sql/prompts.py`
- Modify: `tests/test_table_description.py` ou novo assert em `test_multi_shard.py` / smoke existente que checa prompt

- [ ] **Step 1: Atualizar `_section_sharding`**

Após o protocolo single, acrescentar:

```
5. Se a pergunta envolve 2 ou mais valores do discriminador (explícitos ou
   descobertos via query em tabela NÃO shardada):
   a. Obtenha a lista completa de valores.
   b. Chame materialize_sharded_table(table_id, discriminator_values) UMA vez.
   c. Em seguida consulte com sql_db_query usando o NOME LÓGICO da tabela
      (ex.: recebiveis), NÃO os nomes físicos.
   d. Se o retorno indicar truncated=true, avise o usuário na resposta final.
6. NUNCA chame materialize_sharded_table com 0 ou 1 valor.
7. Fan-out cego (sem lista de discriminadores) continua PROIBIDO.
```

- [ ] **Step 2: Teste**

```python
def test_prompt_mentions_materialize_sharded_table():
    # build prompt com config shardada do exemplo mínimo
    assert "materialize_sharded_table" in prompt
    assert "NOME LÓGICO" in prompt or "nome lógico" in prompt.lower()
```

- [ ] **Step 3: Commit**

```bash
git add txt2sql/prompts.py tests/test_multi_shard.py
git commit -m "feat(prompt): document multi-shard fan-in protocol"
```

---

### Task 6: Docs + exemplo + smoke

**Files:**
- Modify: `docs/adr/0002-sharding-deterministico-sem-fanout.md`
- Modify: `docs/referencia/api.md`
- Modify: `examples/recebiveis.yaml`
- Modify: `docs/arquitetura.md` (mencionar tool se houver lista de tools)
- Modify: `smoke_test_graph.py` (script opcional multi)
- Modify: spec status → implementado ao final

- [ ] **Step 1: ADR-0002**

Trocar negativa “orquestração fora da lib” por:

```
- Cross-shard com lista conhecida: fan-in via DuckDB (`materialize_sharded_table`);
  fan-out cego continua proibido.
```

- [ ] **Step 2: api.md**

Documentar tool e `agent.max_shard_discriminators`.

- [ ] **Step 3: recebiveis.yaml**

```yaml
agent:
  max_shard_discriminators: 20
```

- [ ] **Step 4: Smoke graph (opcional mas desejável)**

Scriptar LLM falso: materialize_sharded_table com 2 CNPJs → sql_db_query agregação no lógico. Se infra de smoke for pesada, garantir cobertura unitária forte e pular extensão do smoke.

- [ ] **Step 5: Suite completa + commit**

```bash
.venv/bin/pytest tests/ -v
.venv/bin/ruff check .
git add -u docs/ examples/ smoke_test_graph.py docs/superpowers/specs/2026-07-27-multi-shard-duckdb-design.md
git commit -m "docs: multi-shard DuckDB fan-in ADR and API"
```

- [ ] **Step 6: Atualizar status da spec para `implementado`**

---

## Spec coverage checklist

| Spec item | Task |
| --- | --- |
| Append multi-fonte DuckDB | 1 |
| `max_shard_discriminators` | 2 |
| Tool + orquestração + truncate + group + filter | 3–4 |
| Estado `multi_materialized` + roteamento lógico | 4 |
| Prompt multi | 5 |
| ADR/API/exemplo | 6 |
| Sem `_shard_key` / single intacto | 1 (idempotência) + 4 |
| Recusa len 0/1 | 3 |
