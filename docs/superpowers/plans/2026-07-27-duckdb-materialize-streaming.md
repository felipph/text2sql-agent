# DuckDB materialize streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fazer `DuckDBSession.materialize` gravar linhas da origem no DuckDB em lotes (`fetchmany` + `executemany`), sem carregar o resultado inteiro em memória Python.

**Architecture:** Manter a API pública. Introduzir `BATCH_SIZE = 5_000`. O primeiro lote define o schema (`_infer_schema`); os demais só inserem. Helpers estreitos substituem `_create_table_from_rows`. Sem novas dependências e sem `batch_size` em config.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.x, DuckDB, pytest.

**Note:** Este workspace atualmente não tem `.git`. Pule os passos de `git commit` até o repositório existir; o restante do plano vale igual.

---

## File map

| File | Responsibility |
| --- | --- |
| `txt2sql/db/duckdb_layer.py` | Constante `BATCH_SIZE`, reescrita de `materialize`, helpers de create/insert |
| `tests/test_duckdb_layer.py` | Testes unitários (streaming multi-lote, vazio, idempotência, `fetch_limit`) |
| `docs/superpowers/specs/2026-07-27-duckdb-materialize-streaming-design.md` | Spec (não editar nesta implementação) |

---

### Task 1: Teste falhando — materialização em múltiplos lotes

**Files:**
- Create: `tests/test_duckdb_layer.py`

- [ ] **Step 1: Criar o teste que exige N linhas > BATCH_SIZE e count correto**

```python
"""Testes da camada DuckDB intermediária."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from txt2sql.config import DuckDBConfig, TableConfig
from txt2sql.db import duckdb_layer
from txt2sql.db.duckdb_layer import DuckDBSession


def _source_engine_with_rows(n: int):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE origem (id INTEGER, valor REAL)"))
        conn.execute(
            text("INSERT INTO origem (id, valor) VALUES (:id, :valor)"),
            [{"id": i, "valor": float(i)} for i in range(n)],
        )
    return engine


def _table(fetch_limit: int = 100_000) -> TableConfig:
    return TableConfig(
        id="origem_logica",
        database="db",
        name="origem",
        duckdb=DuckDBConfig(enabled=True, trigger="always", fetch_limit=fetch_limit),
    )


def test_materialize_streams_multiple_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duckdb_layer, "BATCH_SIZE", 10)
    n = 25  # > BATCH_SIZE → pelo menos 3 lotes
    engine = _source_engine_with_rows(n)
    session = DuckDBSession()
    try:
        session.materialize(_table(), engine, physical_name="origem")
        rows = session.execute("SELECT COUNT(*) AS c FROM origem_logica")
        assert rows[0]["c"] == n
        total = session.execute("SELECT SUM(valor) AS s FROM origem_logica")
        assert total[0]["s"] == sum(range(n))
    finally:
        session.close()
```

- [ ] **Step 2: Rodar o teste e confirmar que falha**

Run:

```bash
cd /home/felipph/pessoal/txt2sql && .venv/bin/pytest tests/test_duckdb_layer.py::test_materialize_streams_multiple_batches -v
```

Expected: FAIL com `AttributeError: module ... has no attribute 'BATCH_SIZE'` (ou similar — ainda não existe a constante / o streaming).

- [ ] **Step 3: Commit (pule se não houver git)**

```bash
git add tests/test_duckdb_layer.py
git commit -m "test: add failing materialize multi-batch coverage"
```

---

### Task 2: Implementar streaming em `materialize`

**Files:**
- Modify: `txt2sql/db/duckdb_layer.py`

- [ ] **Step 1: Adicionar `BATCH_SIZE` e reescrever helpers + `materialize`**

No topo do módulo (após os imports / junto aos regex), adicionar:

```python
BATCH_SIZE = 5_000
```

Substituir `_create_table_from_rows` pelos helpers abaixo e reescrever `materialize` para o fluxo em lotes. Manter `_infer_schema` e o restante da classe (`execute`, `close`, `needs_duckdb`) intactos.

Substituir o método `materialize` e `_create_table_from_rows` por:

```python
    def materialize(
        self,
        table_config: TableConfig,
        source_engine: Engine,
        physical_name: str | None = None,
        filter_sql: str | None = None,
    ) -> None:
        """Materializa as linhas brutas de uma tabela de origem no DuckDB.

        Args:
            table_config: Configuração da tabela volumétrica.
            source_engine: Engine SQLAlchemy do banco de origem.
            physical_name: Nome físico real da tabela na origem (para shards).
                Se ``None``, usa ``table_config.qualified_name``.
            filter_sql: Cláusula ``WHERE`` opcional (sem a palavra ``WHERE``)
                para reduzir o volume trazido do banco de origem.

        A tabela DuckDB criada usa o nome lógico ``table_config.id`` para que a
        query analítica original (reescrita para o nome lógico) funcione.
        """
        logical_name = table_config.id
        if logical_name in self._materialized:
            logger.debug("Tabela {!r} já materializada; pulando", logical_name)
            return

        source_name = physical_name or table_config.qualified_name
        fetch_limit = table_config.duckdb.fetch_limit if table_config.duckdb else 100_000

        where_part = f" WHERE {filter_sql}" if filter_sql else ""
        select_sql = f"SELECT * FROM {source_name}{where_part} LIMIT {fetch_limit}"

        logger.info(
            "Materializando {!r} no DuckDB a partir de {!r} (limit={})",
            logical_name,
            source_name,
            fetch_limit,
        )

        total_rows = 0
        with source_engine.connect() as conn:
            result = conn.execute(text(select_sql))
            columns = list(result.keys())
            first_batch = [tuple(r) for r in result.fetchmany(BATCH_SIZE)]

            if not first_batch:
                self._create_empty_table(logical_name, columns)
            else:
                self._conn.execute(
                    f'CREATE TABLE "{logical_name}" ({self._infer_schema(columns, first_batch)})'
                )
                self._insert_batch(logical_name, columns, first_batch)
                total_rows += len(first_batch)

                while True:
                    batch = [tuple(r) for r in result.fetchmany(BATCH_SIZE)]
                    if not batch:
                        break
                    self._insert_batch(logical_name, columns, batch)
                    total_rows += len(batch)

        self._materialized.add(logical_name)
        logger.info("Tabela {!r} materializada com {} linha(s)", logical_name, total_rows)

    def _create_empty_table(self, name: str, columns: list[str]) -> None:
        """Cria tabela DuckDB vazia com colunas VARCHAR."""
        col_defs = ", ".join(f'"{c}" VARCHAR' for c in columns)
        self._conn.execute(f'CREATE TABLE "{name}" ({col_defs})')

    def _insert_batch(
        self, name: str, columns: list[str], rows: list[tuple[Any, ...]]
    ) -> None:
        """Insere um lote de linhas na tabela DuckDB."""
        if not rows:
            return
        col_defs = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(["?"] * len(columns))
        self._conn.executemany(
            f'INSERT INTO "{name}" ({col_defs}) VALUES ({placeholders})',
            rows,
        )
```

Remover o método antigo `_create_table_from_rows` por completo (não deixar código morto).

Atualizar o docstring do módulo (linhas 7–9) para refletir streaming:

```python
1. Buscamos as linhas brutas do banco de origem (``SELECT *`` simples, com
   ``fetch_limit``), em lotes.
2. Materializamos esses lotes em uma tabela DuckDB *in-memory* com o mesmo nome
   lógico.
```

- [ ] **Step 2: Rodar o teste da Task 1**

Run:

```bash
cd /home/felipph/pessoal/txt2sql && .venv/bin/pytest tests/test_duckdb_layer.py::test_materialize_streams_multiple_batches -v
```

Expected: PASS

- [ ] **Step 3: Commit (pule se não houver git)**

```bash
git add txt2sql/db/duckdb_layer.py
git commit -m "fix: stream DuckDB materialize in batches"
```

---

### Task 3: Testes de borda — vazio, idempotência, fetch_limit

**Files:**
- Modify: `tests/test_duckdb_layer.py`

- [ ] **Step 1: Acrescentar os três testes**

No final de `tests/test_duckdb_layer.py`, adicionar:

```python
def test_materialize_empty_table() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE origem (id INTEGER, nome TEXT)"))
    session = DuckDBSession()
    try:
        session.materialize(_table(), engine, physical_name="origem")
        rows = session.execute("SELECT COUNT(*) AS c FROM origem_logica")
        assert rows[0]["c"] == 0
        assert session.is_materialized("origem_logica")
    finally:
        session.close()


def test_materialize_is_idempotent() -> None:
    engine = _source_engine_with_rows(3)
    session = DuckDBSession()
    try:
        cfg = _table()
        session.materialize(cfg, engine, physical_name="origem")
        session.materialize(cfg, engine, physical_name="origem")
        rows = session.execute("SELECT COUNT(*) AS c FROM origem_logica")
        assert rows[0]["c"] == 3
    finally:
        session.close()


def test_materialize_respects_fetch_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duckdb_layer, "BATCH_SIZE", 10)
    engine = _source_engine_with_rows(50)
    session = DuckDBSession()
    try:
        session.materialize(_table(fetch_limit=15), engine, physical_name="origem")
        rows = session.execute("SELECT COUNT(*) AS c FROM origem_logica")
        assert rows[0]["c"] == 15
    finally:
        session.close()
```

- [ ] **Step 2: Rodar a suíte do arquivo**

Run:

```bash
cd /home/felipph/pessoal/txt2sql && .venv/bin/pytest tests/test_duckdb_layer.py -v
```

Expected: 4 passed

- [ ] **Step 3: Commit (pule se não houver git)**

```bash
git add tests/test_duckdb_layer.py
git commit -m "test: cover empty, idempotent, and fetch_limit materialize"
```

---

### Task 4: Verificação smoke existente

**Files:**
- (nenhuma alteração de código esperada)

- [ ] **Step 1: Rodar o smoke da camada DuckDB**

Run:

```bash
cd /home/felipph/pessoal/txt2sql && .venv/bin/python smoke_test.py
```

Expected: seção `== DuckDB layer ==` com checks OK, incluindo `DuckDB materializa e agrega`; script termina sem traceback.

- [ ] **Step 2: Confirmar que não restou `_create_table_from_rows`**

Run:

```bash
cd /home/felipph/pessoal/txt2sql && rg "_create_table_from_rows|fetchall" txt2sql/db/duckdb_layer.py
```

Expected: sem `_create_table_from_rows`; `fetchall` só em `execute` (resultado analítico), não em `materialize`.

- [ ] **Step 3: Commit final se houver mudanças residuais (pule se não houver git)**

```bash
git status
# se limpo, nada a fazer; se houver docs/ajustes:
# git add -u && git commit -m "chore: finish DuckDB materialize streaming"
```

---

## Spec coverage checklist

| Spec requirement | Task |
| --- | --- |
| `fetchmany` + insert por lote | Task 2 |
| `BATCH_SIZE = 5_000` constante interna | Task 2 |
| Schema do 1º lote / VARCHAR se vazio | Task 2 + Task 3 empty |
| Contador só para log, sem lista total | Task 2 |
| API pública / sem deps novas / sem config batch | Task 2 (não toca config) |
| Teste N > BATCH_SIZE + count | Task 1 |
| Smoke continua passando | Task 4 |
| Idempotência preservada | Task 3 |
