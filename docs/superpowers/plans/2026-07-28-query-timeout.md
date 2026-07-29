# Query Timeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Timeout de execução configurável e independente de driver para o caminho OLTP de `sql_db_query`.

**Architecture:** Deadline no cliente via thread worker + `join(timeout)` em `DatabaseRegistry.execute`. Config: `agent.query_timeout` (default 30) com override `databases[].query_timeout`. Estouro → `QueryTimeoutError` → `ToolMessage` amigável; cancel/invalidate best-effort.

**Tech Stack:** Python 3.12+, SQLAlchemy, threading, pytest.

**Spec:** `docs/superpowers/specs/2026-07-28-query-timeout-design.md`

**Note:** Commits só se o usuário pedir.

---

## File map

| File | Responsibility |
| --- | --- |
| `txt2sql/config.py` | Campos `query_timeout` + validação + parse YAML |
| `txt2sql/db/registry.py` | `QueryTimeoutError`, resolução efetiva, `execute` com deadline |
| `txt2sql/agent.py` | Captura timeout → ToolMessage no ramo OLTP |
| `txt2sql/__init__.py` | Export `QueryTimeoutError` |
| `tests/test_query_timeout.py` | Config + registry + agente |
| `docs/guias/configuracao.md` | Documentar campo |
| `docs/referencia/api.md` | Documentar campo / exceção |
| `playground/config.yaml` | Opcional: declarar `query_timeout` explícito |

---

### Task 1: Config — campos + validação (TDD)

**Files:**
- Modify: `txt2sql/config.py`
- Create: `tests/test_query_timeout.py`

- [ ] **Step 1: Testes falhando de config**

Criar `tests/test_query_timeout.py`:

```python
"""Timeout de execução em sql_db_query (config + registry + agente)."""

from __future__ import annotations

from pathlib import Path

import pytest

from txt2sql.config import AgentConfig, DatabaseConfig, load_config


def test_load_config_query_timeout_default(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db_main\n"
        "    connection_string: sqlite:///:memory:\n"
        "tables: []\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.query_timeout == 30
    assert cfg.databases[0].query_timeout is None


def test_load_config_query_timeout_override(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db_main\n"
        "    connection_string: sqlite:///:memory:\n"
        "    query_timeout: 60\n"
        "agent:\n"
        "  query_timeout: 15\n"
        "tables: []\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.query_timeout == 15
    assert cfg.databases[0].query_timeout == 60


def test_load_config_query_timeout_zero_allowed(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db_main\n"
        "    connection_string: sqlite:///:memory:\n"
        "agent:\n"
        "  query_timeout: 0\n"
        "tables: []\n",
        encoding="utf-8",
    )
    assert load_config(p).query_timeout == 0


def test_load_config_query_timeout_negative_rejected(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db_main\n"
        "    connection_string: sqlite:///:memory:\n"
        "agent:\n"
        "  query_timeout: -1\n"
        "tables: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="query_timeout"):
        load_config(p)


def test_load_config_db_query_timeout_negative_rejected(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "databases:\n"
        "  - id: db_main\n"
        "    connection_string: sqlite:///:memory:\n"
        "    query_timeout: -5\n"
        "tables: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="query_timeout"):
        load_config(p)


def test_effective_query_timeout_inheritance() -> None:
    cfg = AgentConfig(
        databases=[DatabaseConfig(id="db_a", connection_string="sqlite:///:memory:")],
        query_timeout=30,
    )
    assert cfg.effective_query_timeout("db_a") == 30

    cfg2 = AgentConfig(
        databases=[
            DatabaseConfig(
                id="db_b",
                connection_string="sqlite:///:memory:",
                query_timeout=60,
            )
        ],
        query_timeout=30,
    )
    assert cfg2.effective_query_timeout("db_b") == 60
```

- [ ] **Step 2: Rodar testes — devem falhar**

Run: `.venv/bin/pytest tests/test_query_timeout.py -v -k "load_config or effective"`

Expected: FAIL (`query_timeout` / `effective_query_timeout` ausentes)

- [ ] **Step 3: Implementar campos + validação + helper**

Em `DatabaseConfig`, adicionar:

```python
query_timeout: int | None = None
```

Atualizar docstring de `DatabaseConfig` com o atributo.

Em `AgentConfig`, adicionar:

```python
query_timeout: int = 30
```

Atualizar docstring de `AgentConfig`.

Em `AgentConfig._validate`, após a checagem de `max_shard_discriminators`:

```python
if self.query_timeout < 0:
    raise ValueError(
        f"query_timeout deve ser >= 0, recebido: {self.query_timeout}"
    )
for db in self.databases:
    if db.query_timeout is not None and db.query_timeout < 0:
        raise ValueError(
            f"databases[{db.id!r}].query_timeout deve ser >= 0, "
            f"recebido: {db.query_timeout}"
        )
```

Em `AgentConfig`, método:

```python
def effective_query_timeout(self, database_id: str) -> int:
    """Timeout efetivo de execução (segundos) para um banco.

    Override por banco se definido; senão o global ``query_timeout``.
    ``0`` desliga o deadline.
    """
    db = self.get_database(database_id)
    if db.query_timeout is not None:
        return db.query_timeout
    return self.query_timeout
```

(`get_database` já existe; se o nome for outro, usar o índice `_db_index` / método existente.)

Em `load_config`, ao construir `DatabaseConfig`:

```python
query_timeout=(
    int(db["query_timeout"]) if db.get("query_timeout") is not None else None
),
```

Ao construir `AgentConfig`:

```python
query_timeout=int(agent_raw.get("query_timeout", 30)),
```

- [ ] **Step 4: Rodar testes — devem passar**

Run: `.venv/bin/pytest tests/test_query_timeout.py -v -k "load_config or effective"`

Expected: PASS

---

### Task 2: Registry — `QueryTimeoutError` + execute com deadline (TDD)

**Files:**
- Modify: `txt2sql/db/registry.py`
- Modify: `txt2sql/__init__.py`
- Modify: `tests/test_query_timeout.py`

- [ ] **Step 1: Testes falhando de registry**

Acrescentar em `tests/test_query_timeout.py`:

```python
import time
from typing import Any
from unittest.mock import MagicMock

from txt2sql.db.registry import DatabaseRegistry, QueryTimeoutError


def _registry_with_timeout(query_timeout: int) -> DatabaseRegistry:
    cfg = AgentConfig(
        databases=[
            DatabaseConfig(
                id="db_main",
                connection_string="sqlite:///:memory:",
                read_only=False,
            )
        ],
        query_timeout=query_timeout,
    )
    return DatabaseRegistry(cfg)


def test_execute_raises_query_timeout(monkeypatch: Any) -> None:
    registry = _registry_with_timeout(1)

    def slow_execute(*args: Any, **kwargs: Any) -> Any:
        time.sleep(3)
        raise AssertionError("não deveria completar")

    # Injeta conexão fake cujo execute bloqueia além do timeout
    fake_conn = MagicMock()
    fake_conn.execute.side_effect = slow_execute
    fake_cm = MagicMock()
    fake_cm.__enter__.return_value = fake_conn
    fake_cm.__exit__.return_value = False

    engine = MagicMock()
    engine.connect.return_value = fake_cm
    registry._engines["db_main"] = engine

    with pytest.raises(QueryTimeoutError, match="timeout"):
        registry.execute("db_main", "SELECT 1")

    # best-effort: conexão invalidada ou fechada
    assert fake_conn.invalidate.called or fake_conn.close.called


def test_execute_timeout_disabled_completes(monkeypatch: Any) -> None:
    registry = _registry_with_timeout(0)

    result_proxy = MagicMock()
    result_proxy.keys.return_value = ["x"]
    result_proxy.fetchall.return_value = [(1,)]

    fake_conn = MagicMock()
    fake_conn.execute.return_value = result_proxy
    fake_cm = MagicMock()
    fake_cm.__enter__.return.return_value = fake_conn  # noqa — fix below
```

Corrigir o mock do context manager no segundo teste (escrever assim):

```python
def test_execute_timeout_disabled_completes() -> None:
    registry = _registry_with_timeout(0)

    result_proxy = MagicMock()
    result_proxy.keys.return_value = ["x"]
    result_proxy.fetchall.return_value = [(1,)]

    fake_conn = MagicMock()
    fake_conn.execute.return_value = result_proxy

    class _CM:
        def __enter__(self) -> Any:
            return fake_conn

        def __exit__(self, *args: Any) -> bool:
            return False

    engine = MagicMock()
    engine.connect.return_value = _CM()
    registry._engines["db_main"] = engine

    rows = registry.execute("db_main", "SELECT 1")
    assert rows == [{"x": 1}]


def test_execute_within_timeout_returns_rows() -> None:
    registry = _registry_with_timeout(5)

    result_proxy = MagicMock()
    result_proxy.keys.return_value = ["n"]
    result_proxy.fetchall.return_value = [(42,)]

    fake_conn = MagicMock()
    fake_conn.execute.return_value = result_proxy

    class _CM:
        def __enter__(self) -> Any:
            return fake_conn

        def __exit__(self, *args: Any) -> bool:
            return False

    engine = MagicMock()
    engine.connect.return_value = _CM()
    registry._engines["db_main"] = engine

    rows = registry.execute("db_main", "SELECT 42")
    assert rows == [{"n": 42}]
```

- [ ] **Step 2: Rodar testes — devem falhar**

Run: `.venv/bin/pytest tests/test_query_timeout.py -v -k "execute"`

Expected: FAIL (`QueryTimeoutError` ausente ou `execute` sem deadline)

- [ ] **Step 3: Implementar em `registry.py`**

Adicionar imports:

```python
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
```

Exceção:

```python
class QueryTimeoutError(Exception):
    """Query SELECT excedeu o ``query_timeout`` configurado."""

    def __init__(self, database_id: str, timeout: int) -> None:
        self.database_id = database_id
        self.timeout = timeout
        super().__init__(
            f"Query no banco {database_id!r} excedeu o timeout de {timeout}s"
        )
```

Helper de cancel best-effort (método estático/privado):

```python
@staticmethod
def _cancel_connection(conn: Connection) -> None:
    """Tenta cancelar a query e invalidar a conexão (best-effort)."""
    try:
        dbapi = getattr(conn, "connection", None)
        raw = getattr(dbapi, "dbapi_connection", None) or getattr(
            dbapi, "driver_connection", None
        )
        cancel = getattr(raw, "cancel", None) if raw is not None else None
        if callable(cancel):
            cancel()
    except Exception:  # noqa: BLE001
        logger.debug("cancel do driver falhou (best-effort)")
    try:
        conn.invalidate()
    except Exception:  # noqa: BLE001
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            logger.debug("invalidate/close falhou (best-effort)")
```

Reescrever `execute`:

```python
def execute(self, database_id: str, sql: str) -> list[dict[str, Any]]:
    """Executa uma query e retorna as linhas como lista de dicts.

    Respeita ``AgentConfig.effective_query_timeout``: se > 0, aplica
    deadline no cliente (thread + join). Estouro →
    :class:`QueryTimeoutError` após cancel/invalidate best-effort.
    """
    timeout = self._config.effective_query_timeout(database_id)
    engine = self.get_engine(database_id)

    if timeout == 0:
        with engine.connect() as conn:
            return self._fetch_dicts(conn, sql)

    conn = engine.connect()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._fetch_dicts, conn, sql)
            try:
                return future.result(timeout=timeout)
            except FuturesTimeout as err:
                self._cancel_connection(conn)
                raise QueryTimeoutError(database_id, timeout) from err
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


@staticmethod
def _fetch_dicts(conn: Connection, sql: str) -> list[dict[str, Any]]:
    result = conn.execute(text(sql))
    columns = list(result.keys())
    return [dict(zip(columns, row)) for row in result.fetchall()]
```

Atualizar `__all__`:

```python
__all__ = ["DatabaseRegistry", "ReadOnlyViolationError", "QueryTimeoutError"]
```

Em `txt2sql/__init__.py`:

```python
from txt2sql.db.registry import QueryTimeoutError

__all__ = [
    "build_agent",
    "AgentConfig",
    "load_config",
    "ShardResult",
    "QueryTimeoutError",
]
```

Atualizar docstring do pacote se listar API pública.

- [ ] **Step 4: Rodar testes — devem passar**

Run: `.venv/bin/pytest tests/test_query_timeout.py -v`

Expected: PASS (inclui config + registry). Ajustar asserts de `invalidate`/`close` se o mock precisar de `spec` diferente — o importante é que `_cancel_connection` seja chamado no timeout.

**Nota de implementação:** `ThreadPoolExecutor` + `future.result(timeout=)` é preferível a `threading.Thread`+`join` porque propaga a exceção do worker e encaixa no padrão da stdlib. O worker pode continuar até o driver liberar após invalidate; isso é aceitável (spec: best-effort).

---

### Task 3: Agente — ToolMessage amigável (TDD)

**Files:**
- Modify: `txt2sql/agent.py`
- Modify: `tests/test_query_timeout.py`

- [ ] **Step 1: Teste falhando do grafo**

Acrescentar em `tests/test_query_timeout.py` (padrão de `tests/test_multi_query.py`):

```python
import os
import sqlite3
import tempfile
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

import txt2sql.agent as agent_mod
from txt2sql.config import ColumnConfig, TableConfig
from txt2sql.intent import IntentPlan, MetricClause


class ScriptedLLM:
    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self._i = 0

    def bind_tools(self, tools: list[Any]) -> ScriptedLLM:
        return self

    def with_structured_output(self, schema: Any) -> ScriptedLLM:
        return self

    def invoke(self, messages: list[Any]) -> Any:
        msg = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return msg


def test_execute_queries_timeout_becomes_tool_message(
    monkeypatch: Any,
) -> None:
    tmp = tempfile.mkdtemp()
    main_db = Path(tmp) / "main.db"
    c = sqlite3.connect(main_db)
    c.executescript(
        "CREATE TABLE clientes (cnpj TEXT, razao_social TEXT);"
        "INSERT INTO clientes VALUES ('111', 'Alpha');"
    )
    c.commit()
    c.close()

    os.environ.update(
        AZURE_OPENAI_DEPLOYMENT="gpt-4o",
        AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com/",
        AZURE_OPENAI_API_KEY="dummy",
    )

    cfg = AgentConfig(
        databases=[
            DatabaseConfig(
                id="db_main",
                connection_string=f"sqlite:///{main_db}",
            )
        ],
        tables=[
            TableConfig(
                id="clientes",
                database="db_main",
                name="clientes",
                columns=[
                    ColumnConfig(name="cnpj"),
                    ColumnConfig(name="razao_social"),
                ],
            )
        ],
        dialect=None,
        query_timeout=1,
    )

    def boom(database_id: str, sql: str) -> list[dict[str, Any]]:
        raise QueryTimeoutError(database_id, 1)

    ready = IntentPlan(
        status="ready",
        question_rewrite="nome do cliente 111",
        metrics=[
            MetricClause(
                table_id="clientes",
                column_id="razao_social",
                agg="none",
            )
        ],
    )
    script = [
        ready,
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "sql_db_query",
                    "args": {
                        "query": "SELECT razao_social FROM clientes WHERE cnpj = '111'"
                    },
                    "id": "q1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content="Não consegui a tempo."),
    ]
    monkeypatch.setattr(agent_mod, "build_llm", lambda config: ScriptedLLM(script))

    agent = agent_mod.build_agent(cfg, checkpointer=MemorySaver())

    # Patch registry.execute do agente: intercepta via monkeypatch no módulo
    # após build — build_agent fecha o registry; patch na instância:
    # reimplementação: monkeypatch DatabaseRegistry.execute antes do build
    monkeypatch.setattr(
        "txt2sql.db.registry.DatabaseRegistry.execute",
        boom,
    )
    # rebuild para capturar o método patched se o nó já fechou referência
    agent = agent_mod.build_agent(cfg, checkpointer=MemorySaver())

    result = agent.invoke(
        {"messages": [HumanMessage(content="nome?")]},
        config={"configurable": {"thread_id": "timeout-q"}},
    )
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert tool_msgs, "esperava ToolMessage de timeout"
    assert any("timeout" in str(m.content).lower() for m in tool_msgs)
    assert result.get("page_count", 0) >= 1
    # grafo não abortou: houve resposta final do LLM
    assert any(
        isinstance(m, AIMessage) and m.content == "Não consegui a tempo."
        for m in result["messages"]
    )
```

Se `build_agent` captura `registry.execute` por closure no nó, o monkeypatch em `DatabaseRegistry.execute` **antes** do `build_agent` é suficiente (o nó chama `registry.execute`, método de instância que resolve dinamicamente). Preferir um único `build_agent` após o patch:

```python
monkeypatch.setattr(DatabaseRegistry, "execute", boom)
agent = agent_mod.build_agent(cfg, checkpointer=MemorySaver())
```

Importar `DatabaseRegistry` e `QueryTimeoutError` no topo do arquivo de teste.

- [ ] **Step 2: Rodar teste — deve falhar**

Run: `.venv/bin/pytest tests/test_query_timeout.py::test_execute_queries_timeout_becomes_tool_message -v`

Expected: FAIL (ToolMessage genérico `ERRO ao executar a query: ...` ou exceção não tratada — ainda sem mensagem amigável específica de timeout)

- [ ] **Step 3: Capturar no agente**

Em `txt2sql/agent.py`, importar:

```python
from txt2sql.db.registry import DatabaseRegistry, QueryTimeoutError, ReadOnlyViolationError
```

(ajustar imports existentes — hoje `ReadOnlyViolationError` pode vir de `guardrail` ou `registry`; unificar sem duplicar.)

No ramo OLTP de `execute_queries`, trocar o `except` para:

```python
try:
    rows = registry.execute(database_id, sql)
    content = _rows_to_text(rows, config.max_string_length, config.top_k)
except QueryTimeoutError as err:
    logger.warning("execute_queries: timeout OLTP: {}", err)
    content = (
        f"ERRO: query excedeu o timeout de {err.timeout} segundos. "
        "Simplifique a consulta ou filtre mais."
    )
except (sa_exc.SQLAlchemyError, ReadOnlyViolationError) as err:
    logger.warning("execute_queries: erro OLTP: {}", err)
    content = f"ERRO ao executar a query: {err}"
```

- [ ] **Step 4: Rodar testes — devem passar**

Run: `.venv/bin/pytest tests/test_query_timeout.py -v`

Expected: PASS

Run: `.venv/bin/pytest tests/ -v`

Expected: PASS (regressão)

---

### Task 4: Docs + playground

**Files:**
- Modify: `docs/guias/configuracao.md`
- Modify: `docs/referencia/api.md`
- Modify: `docs/arquitetura.md` (uma linha no fluxo / componentes)
- Modify: `playground/config.yaml` (opcional, explícito)
- Modify: `docs/superpowers/specs/2026-07-28-query-timeout-design.md` (status → implementado ao final)

- [ ] **Step 1: Atualizar guia de configuração**

Em `docs/guias/configuracao.md`, na tabela do bloco `agent`, incluir `query_timeout` (default 30). Na seção de `databases`, mencionar override opcional `query_timeout`.

- [ ] **Step 2: Atualizar API**

Em `docs/referencia/api.md`:

- documentar `AgentConfig.query_timeout` / `DatabaseConfig.query_timeout` / `effective_query_timeout`;
- documentar `QueryTimeoutError` na API pública.

- [ ] **Step 3: Arquitetura**

Em `docs/arquitetura.md`, na descrição de `DatabaseRegistry` ou no passo de `sql_db_query`, uma frase: execução OLTP respeita `query_timeout` (deadline no cliente).

- [ ] **Step 4: Playground (opcional)**

Em `playground/config.yaml`, sob `agent:`:

```yaml
  query_timeout: 30
```

- [ ] **Step 5: Marcar spec**

Status da spec: `implementado`.

- [ ] **Step 6: Verificação final**

Run: `.venv/bin/ruff check txt2sql/config.py txt2sql/db/registry.py txt2sql/agent.py txt2sql/__init__.py tests/test_query_timeout.py`

Run: `.venv/bin/pytest tests/test_query_timeout.py tests/test_multi_query.py -v`

Expected: PASS / limpo

---

## Spec coverage (self-review)

| Requisito da spec | Task |
| --- | --- |
| `agent.query_timeout` default 30 | Task 1 |
| Override `databases[].query_timeout` | Task 1 |
| `0` desliga | Task 1 + 2 |
| Negativo → ValueError | Task 1 |
| Só OLTP `registry.execute` | Task 2–3 |
| Thread/deadline cliente | Task 2 (`ThreadPoolExecutor`) |
| Cancel best-effort | Task 2 `_cancel_connection` |
| `QueryTimeoutError` + export `__init__` | Task 2 |
| ToolMessage amigável | Task 3 |
| Conta em `page_count` | Task 3 (mesmo fluxo pós-`out.append`) |
| Sem DuckDB/materialize/schema | respeitado (fora das tasks) |
| Docs | Task 4 |
