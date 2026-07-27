# Streamlit Playground Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Entregar um harness em `playground/` com Postgres (1 main + 2 shards), script de seed, UI Streamlit de chat+debug e perguntas prontas — sem alterar a lib `txt2sql`.

**Architecture:** Compose sobe três Postgres com SQL versionado em `seed/`. `seed_data.py` gera esse SQL (`--dump-sql`) e reaplica (`--apply`). Streamlit local carrega `config.yaml`, `build_agent` + `MemorySaver`, e `debug_view.py` extrai tool calls das mensagens do turno.

**Tech Stack:** Python 3.12+, Streamlit, Postgres 16 (Docker), SQLAlchemy + psycopg, LangGraph MemorySaver, PyYAML.

---

## File map

| File | Responsibility |
| --- | --- |
| `playground/__init__.py` | Pacote importável (`playground.shard_resolver`) |
| `playground/shard_resolver.py` | Resolução CNPJ → 2 shards |
| `playground/seed_data.py` | Dataset canônico, `--dump-sql`, `--apply`, gabarito |
| `playground/seed/*.sql` | Init SQL versionado para compose |
| `playground/docker-compose.yml` | 3 serviços Postgres |
| `playground/.env.example` | URLs e placeholders Azure |
| `playground/config.yaml` | Agent config (2 shards + DuckDB) |
| `playground/prompts.yaml` | Perguntas prontas + expected |
| `playground/debug_view.py` | Parser de tool calls do turno |
| `playground/app.py` | UI Streamlit |
| `playground/README.md` | Setup e gabarito |
| `tests/test_playground_resolver.py` | Testes do resolver |
| `tests/test_playground_seed.py` | Testes dump/gabarito sem Docker |
| `tests/test_playground_debug_view.py` | Testes do parser |
| `pyproject.toml` | Extra `[playground]` |
| `docs/primeiros-passos.md` | Link para o playground |

---

### Task 1: Resolver de shard (2 faixas)

**Files:**
- Create: `playground/__init__.py`
- Create: `playground/shard_resolver.py`
- Create: `tests/test_playground_resolver.py`

- [ ] **Step 1: Escrever testes falhando**

```python
"""Testes do resolver de shard do playground (2 faixas)."""

from __future__ import annotations

import pytest

from playground.shard_resolver import resolve_cnpj_shard


def test_prefix_low_goes_to_shard_1() -> None:
    r = resolve_cnpj_shard("12345678000190")
    assert r.database_id == "db_shard_1"
    assert r.table_name == "recebiveis_123"


def test_prefix_boundary_499_shard_1() -> None:
    r = resolve_cnpj_shard("49900000000100")
    assert r.database_id == "db_shard_1"
    assert r.table_name == "recebiveis_499"


def test_prefix_500_goes_to_shard_2() -> None:
    r = resolve_cnpj_shard("55667788000111")
    assert r.database_id == "db_shard_2"
    assert r.table_name == "recebiveis_556"


def test_prefix_999_shard_2() -> None:
    r = resolve_cnpj_shard("99988877000155")
    assert r.database_id == "db_shard_2"
    assert r.table_name == "recebiveis_999"


def test_invalid_cnpj_raises() -> None:
    with pytest.raises(ValueError, match="14 dígitos"):
        resolve_cnpj_shard("123")
```

- [ ] **Step 2: Rodar e confirmar falha**

Run: `.venv/bin/pytest tests/test_playground_resolver.py -v`  
Expected: FAIL (módulo não encontrado)

- [ ] **Step 3: Implementar**

`playground/__init__.py`: vazio (ou docstring curta).

`playground/shard_resolver.py`:

```python
"""Resolver de shard do playground: 2 faixas por prefixo de CNPJ."""

from __future__ import annotations

from txt2sql.config import ShardResult


def _normalize_cnpj(cnpj: str) -> str:
    digits = "".join(ch for ch in cnpj if ch.isdigit())
    if len(digits) != 14:
        raise ValueError(
            f"CNPJ inválido: esperados 14 dígitos, obtidos {len(digits)} ({cnpj!r})."
        )
    return digits


def resolve_cnpj_shard(cnpj: str) -> ShardResult:
    """000–499 → db_shard_1; 500–999 → db_shard_2; tabela recebiveis_<prefix>."""
    digits = _normalize_cnpj(cnpj)
    prefix = digits[:3]
    database_id = "db_shard_1" if int(prefix) <= 499 else "db_shard_2"
    return ShardResult(database_id=database_id, table_name=f"recebiveis_{prefix}")
```

- [ ] **Step 4: Rodar testes — PASS**

Run: `.venv/bin/pytest tests/test_playground_resolver.py -v`

- [ ] **Step 5: Commit**

```bash
git add playground/__init__.py playground/shard_resolver.py tests/test_playground_resolver.py
git commit -m "feat(playground): resolver CNPJ com 2 shards"
```

---

### Task 2: Dataset canônico + seed_data.py

**Files:**
- Create: `playground/seed_data.py`
- Create: `tests/test_playground_seed.py`

- [ ] **Step 1: Testes falhando (dump + gabarito, sem Docker)**

```python
"""Testes do gerador de seed do playground."""

from __future__ import annotations

from pathlib import Path

from playground.seed_data import CLIENTES, GABARITO, RECEBIVEIS, dump_sql, render_gabarito


def test_gabarito_totais() -> None:
    assert GABARITO["12345678000190"] == 175.0
    assert GABARITO["55667788000111"] == 280.0
    assert GABARITO["99988877000155"] == 40.0
    assert GABARITO["acme_beta"] == 455.0


def test_dump_sql_writes_three_files(tmp_path: Path) -> None:
    paths = dump_sql(tmp_path)
    assert set(p.name for p in paths) == {"01_main.sql", "02_shard1.sql", "03_shard2.sql"}
    main = (tmp_path / "01_main.sql").read_text()
    assert "CREATE TABLE" in main and "clientes" in main and "ACME" in main
    s1 = (tmp_path / "02_shard1.sql").read_text()
    assert "recebiveis_123" in s1 and "100" in s1
    s2 = (tmp_path / "03_shard2.sql").read_text()
    assert "recebiveis_556" in s2 and "recebiveis_999" in s2


def test_render_gabarito_mentions_expected_sums() -> None:
    text = render_gabarito()
    assert "175" in text and "280" in text and "455" in text
```

Constantes esperadas no módulo: `CLIENTES` (lista de dicts), `RECEBIVEIS` (lista com cnpj/valor/status/data), `GABARITO` dict.

- [ ] **Step 2: Rodar — FAIL**

Run: `.venv/bin/pytest tests/test_playground_seed.py -v`

- [ ] **Step 3: Implementar `seed_data.py`**

Dataset:

- Clientes: ACME `12345678000190`, Beta `55667788000111`, Gama `99988877000155`
- Recebíveis ACME: (100,pago), (50,pendente), (25,pago)
- Beta: (200,pago), (80,pendente)
- Gama: (40,vencido)
- Datas: usar `2026-01-15`, `2026-02-01`, etc. fixas

Funções públicas:

- `dump_sql(out_dir: Path) -> list[Path]`
- `apply(urls: dict[str, str]) -> None` — keys `main`, `shard1`, `shard2`; usa SQLAlchemy `create_engine` + `text()`; CREATE IF NOT EXISTS; TRUNCATE; INSERT
- `render_gabarito() -> str`
- `main()` argparse: `--dump-sql [dir]`, `--apply`, lê env `MAIN_DB_URL` / `SHARD_1_DB_URL` / `SHARD_2_DB_URL`

Para `--apply`, converter URL `postgresql+psycopg://` normalmente (SQLAlchemy aceita).

DDL main:

```sql
CREATE TABLE IF NOT EXISTS clientes (
  cnpj VARCHAR(14) PRIMARY KEY,
  razao_social VARCHAR(200) NOT NULL
);
```

DDL shard (por tabela física):

```sql
CREATE TABLE IF NOT EXISTS recebiveis_123 (
  cnpj VARCHAR(14) NOT NULL,
  valor NUMERIC(14,2) NOT NULL,
  data_vencimento DATE NOT NULL,
  status VARCHAR(20) NOT NULL
);
```

- [ ] **Step 4: Testes PASS**

Run: `.venv/bin/pytest tests/test_playground_seed.py -v`

- [ ] **Step 5: Commit**

```bash
git add playground/seed_data.py tests/test_playground_seed.py
git commit -m "feat(playground): script de seed com dump e gabarito"
```

---

### Task 3: Gerar SQL versionado + docker-compose + env

**Files:**
- Create: `playground/seed/01_main.sql`, `02_shard1.sql`, `03_shard2.sql`
- Create: `playground/docker-compose.yml`
- Create: `playground/.env.example`

- [ ] **Step 1: Gerar SQL**

Run:

```bash
.venv/bin/python playground/seed_data.py --dump-sql playground/seed
```

Expected: três arquivos; stdout com gabarito.

- [ ] **Step 2: `docker-compose.yml`**

```yaml
services:
  db_main:
    image: postgres:16
    environment:
      POSTGRES_USER: txt2sql
      POSTGRES_PASSWORD: txt2sql
      POSTGRES_DB: txt2sql
    ports: ["5432:5432"]
    volumes:
      - ./seed/01_main.sql:/docker-entrypoint-initdb.d/01_main.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U txt2sql -d txt2sql"]
      interval: 3s
      timeout: 3s
      retries: 10

  db_shard_1:
    image: postgres:16
    environment:
      POSTGRES_USER: txt2sql
      POSTGRES_PASSWORD: txt2sql
      POSTGRES_DB: txt2sql
    ports: ["5433:5432"]
    volumes:
      - ./seed/02_shard1.sql:/docker-entrypoint-initdb.d/01_shard.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U txt2sql -d txt2sql"]
      interval: 3s
      timeout: 3s
      retries: 10

  db_shard_2:
    image: postgres:16
    environment:
      POSTGRES_USER: txt2sql
      POSTGRES_PASSWORD: txt2sql
      POSTGRES_DB: txt2sql
    ports: ["5434:5432"]
    volumes:
      - ./seed/03_shard2.sql:/docker-entrypoint-initdb.d/01_shard.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U txt2sql -d txt2sql"]
      interval: 3s
      timeout: 3s
      retries: 10
```

- [ ] **Step 3: `.env.example`**

```bash
MAIN_DB_URL=postgresql+psycopg://txt2sql:txt2sql@localhost:5432/txt2sql
SHARD_1_DB_URL=postgresql+psycopg://txt2sql:txt2sql@localhost:5433/txt2sql
SHARD_2_DB_URL=postgresql+psycopg://txt2sql:txt2sql@localhost:5434/txt2sql
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_DEPLOYMENT=gpt-4o
AZURE_OPENAI_API_VERSION=2024-06-01
```

- [ ] **Step 4: Subir compose e validar seed (se Docker disponível)**

```bash
cd playground && docker compose up -d
# aguardar healthy
docker compose exec db_main psql -U txt2sql -c "SELECT COUNT(*) FROM clientes;"
# expected: 3
```

Se Docker indisponível: pular validação runtime; arquivos ficam commitados.

- [ ] **Step 5: Commit**

```bash
git add playground/seed playground/docker-compose.yml playground/.env.example
git commit -m "feat(playground): compose Postgres 1+2 shards com seed SQL"
```

---

### Task 4: config.yaml + prompts.yaml

**Files:**
- Create: `playground/config.yaml`
- Create: `playground/prompts.yaml`

- [ ] **Step 1: `config.yaml`** (baseado em `examples/recebiveis.yaml`, 2 shards)

```yaml
dialect: postgres

databases:
  - id: db_main
    connection_env: MAIN_DB_URL
    read_only: true
    connect_timeout: 10
  - id: db_shard_1
    connection_env: SHARD_1_DB_URL
    read_only: true
  - id: db_shard_2
    connection_env: SHARD_2_DB_URL
    read_only: true

tables:
  - id: clientes
    database: db_main
    schema: public
    name: clientes
    description: >-
      Cadastro de clientes (PJ) com CNPJ e razão social. Não shardada.

  - id: recebiveis
    database: db_main
    schema: public
    name: recebiveis
    description: >-
      Títulos a receber. Shardada por CNPJ em 2 bancos; agregações via DuckDB.
    sharding:
      discriminator_column: cnpj
      resolver: "playground.shard_resolver:resolve_cnpj_shard"
    columns:
      - name: cnpj
        type: VARCHAR
        description: "CNPJ 14 dígitos."
      - name: valor
        type: NUMERIC
        description: "Valor bruto BRL."
      - name: data_vencimento
        type: DATE
        description: "Vencimento YYYY-MM-DD."
      - name: status
        type: VARCHAR
        description: "pendente | pago | vencido"
    duckdb:
      enabled: true
      trigger: aggregation
      fetch_limit: 100000

relationships:
  - from: { table: recebiveis, column: cnpj }
    to: { table: clientes, column: cnpj }
    description: "Recebível pertence ao cliente."

glossary:
  - term: recebível
    definition: "Título de crédito a receber."
  - term: CNPJ
    definition: "14 dígitos, sem formatação."

agent:
  top_k: 50
  max_pages: 10
  max_string_length: 10000
  read_only: true
  max_shard_discriminators: 20

llm:
  deployment: gpt-4o
  api_version: "2024-06-01"
  temperature: 0.0

custom_section: |
  Playground de testes. recebiveis é SHARDADA por CNPJ (2 shards).
  1 CNPJ → resolve_shard; 2+ → materialize_sharded_table + nome lógico.
```

- [ ] **Step 2: `prompts.yaml`**

```yaml
prompts:
  - id: single_sum
    label: Soma CNPJ único (ACME)
    question: Qual a soma dos valores dos recebíveis do CNPJ 12345678000190?
    expected: "175"
    notes: Deve usar resolve_shard → db_shard_1 / recebiveis_123

  - id: multi_sum
    label: Multi-CNPJ ACME + Beta (DuckDB)
    question: Qual a soma total dos recebíveis dos CNPJs 12345678000190 e 55667788000111?
    expected: "455"
    notes: Deve chamar materialize_sharded_table

  - id: join_vencido
    label: Join — cliente com vencido
    question: Qual a razão social do cliente que possui recebível com status vencido?
    expected: "Gama"
    notes: Cruzar clientes + recebiveis

  - id: guardrail_delete
    label: Guardrail — pedido de DELETE
    question: Apague todos os recebíveis do CNPJ 12345678000190.
    expected: "rejeitado / não executar DELETE"
    notes: Guardrail fail-closed deve impedir DML
```

- [ ] **Step 3: Commit**

```bash
git add playground/config.yaml playground/prompts.yaml
git commit -m "feat(playground): config YAML e perguntas prontas"
```

---

### Task 5: debug_view.py

**Files:**
- Create: `playground/debug_view.py`
- Create: `tests/test_playground_debug_view.py`

- [ ] **Step 1: Teste falhando**

```python
"""Testes do extrator de debug do playground."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from playground.debug_view import extract_turn_debug


def test_extract_resolve_and_query() -> None:
    messages = [
        HumanMessage(content="soma?"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "resolve_shard",
                    "args": {"table_id": "recebiveis", "discriminator_value": "12345678000190"},
                    "id": "1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content='{"database_id":"db_shard_1","table_name":"recebiveis_123"}', tool_call_id="1"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "sql_db_query",
                    "args": {"query": "SELECT SUM(valor) FROM recebiveis_123"},
                    "id": "2",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="[{'sum': 175}]", tool_call_id="2"),
        AIMessage(content="A soma é 175."),
    ]
    debug = extract_turn_debug(messages)
    assert len(debug.steps) == 2
    assert debug.steps[0].name == "resolve_shard"
    assert "db_shard_1" in debug.steps[0].result
    assert debug.steps[1].name == "sql_db_query"
    assert "SUM" in (debug.steps[1].args.get("query") or "")
    assert debug.final_answer == "A soma é 175."
    assert debug.looks_like_guardrail_reject is False


def test_guardrail_reject_flag() -> None:
    messages = [
        HumanMessage(content="delete"),
        AIMessage(
            content="",
            tool_calls=[{"name": "sql_db_query", "args": {"query": "DELETE FROM x"}, "id": "1", "type": "tool_call"}],
        ),
        ToolMessage(content="Erro de guardrail: apenas SELECT permitido", tool_call_id="1"),
        AIMessage(content="Não posso apagar."),
    ]
    debug = extract_turn_debug(messages)
    assert debug.looks_like_guardrail_reject is True
```

- [ ] **Step 2: Rodar — FAIL**

- [ ] **Step 3: Implementar**

```python
"""Extrai tool calls / SQL / guardrail das mensagens do turno."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DebugStep:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    result: str = ""


@dataclass
class TurnDebug:
    steps: list[DebugStep] = field(default_factory=list)
    final_answer: str = ""
    looks_like_guardrail_reject: bool = False


_GUARDRAIL_MARKERS = ("guardrail", "não permitido", "nao permitido", "rejeit", "read-only", "apenas select")


def extract_turn_debug(messages: list[Any]) -> TurnDebug:
    """Associa cada ToolMessage ao tool_call precedente pelo tool_call_id."""
    pending: dict[str, DebugStep] = {}
    steps: list[DebugStep] = []
    final = ""
    guardrail = False

    for msg in messages:
        msg_type = getattr(msg, "type", None) or msg.__class__.__name__
        if msg_type in ("ai", "AIMessage") or msg.__class__.__name__ == "AIMessage":
            content = getattr(msg, "content", "") or ""
            tool_calls = getattr(msg, "tool_calls", None) or []
            if tool_calls:
                for tc in tool_calls:
                    step = DebugStep(name=tc.get("name", "?"), args=dict(tc.get("args") or {}))
                    pending[tc.get("id", "")] = step
                    steps.append(step)
            elif content:
                final = content if isinstance(content, str) else str(content)
        elif msg_type in ("tool", "ToolMessage") or msg.__class__.__name__ == "ToolMessage":
            tid = getattr(msg, "tool_call_id", "")
            content = str(getattr(msg, "content", "") or "")
            if tid in pending:
                pending[tid].result = content
            low = content.lower()
            if any(m in low for m in _GUARDRAIL_MARKERS):
                guardrail = True

    return TurnDebug(steps=steps, final_answer=final, looks_like_guardrail_reject=guardrail)
```

- [ ] **Step 4: PASS + commit**

```bash
git add playground/debug_view.py tests/test_playground_debug_view.py
git commit -m "feat(playground): extrator de debug de tool calls"
```

---

### Task 6: app.py Streamlit + extra pyproject

**Files:**
- Create: `playground/app.py`
- Modify: `pyproject.toml` (extra `playground`)

- [ ] **Step 1: Adicionar deps**

Em `pyproject.toml`:

```toml
[project.optional-dependencies]
langfuse = ["langfuse>=2.0.0"]
dev = [
    "pytest>=8.0.0",
    "ruff>=0.6.0",
]
playground = [
    "streamlit>=1.32.0",
    "psycopg[binary]>=3.1.0",
]
```

- [ ] **Step 2: Implementar `app.py`**

Responsabilidades:

- Paths relativos à pasta `playground/`
- Sidebar: ping DBs (`SELECT 1` via SQLAlchemy), thread_id, nova conversa, botões de prompts
- Centro: `st.chat_message` para Human/AI; `st.chat_input`
- Direita: `st.expander` / colunas com `extract_turn_debug`
- `@st.cache_resource` para agent (falha clara se Azure/env ausente)
- Ao clicar prompt: seta `st.session_state.pending_question` + `expected`

Estrutura mínima de colunas: `st.columns([1.2, 2.2, 1.6])` ou sidebar + 2 colunas.

Snippet central:

```python
from pathlib import Path
import uuid
import yaml
import streamlit as st
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine, text

from txt2sql import build_agent, load_config
from playground.debug_view import extract_turn_debug

ROOT = Path(__file__).resolve().parent

# load prompts, ping_db(url), get_agent(), render UI…
```

- [ ] **Step 3: Instalar extra**

```bash
uv sync --extra playground
# ou: .venv/bin/pip install -e ".[playground]"
```

- [ ] **Step 4: Smoke sintático**

```bash
.venv/bin/python -c "import playground.app" 
# pode falhar por streamlit side-effect — preferir:
.venv/bin/python -c "from playground.debug_view import extract_turn_debug; from playground.seed_data import GABARITO; print(GABARITO)"
```

- [ ] **Step 5: Commit**

```bash
git add playground/app.py pyproject.toml
git commit -m "feat(playground): UI Streamlit de chat e debug"
```

---

### Task 7: README + primeiros-passos

**Files:**
- Create: `playground/README.md`
- Modify: `docs/primeiros-passos.md`

- [ ] **Step 1: README** com: pré-requisitos Docker/Azure, `docker compose up -d`, env, `uv sync --extra playground`, `streamlit run playground/app.py`, tabela gabarito, `seed_data.py --apply` / `--dump-sql`.

- [ ] **Step 2: Em `docs/primeiros-passos.md`**, após “Uso mínimo do agente”, adicionar seção curta:

```markdown
## Playground (Postgres + Streamlit)

Para exercitar o agente contra Postgres real com sharding e painel de debug:

veja [playground/README.md](../playground/README.md).
```

- [ ] **Step 3: Rodar suíte de testes do playground**

```bash
.venv/bin/pytest tests/test_playground_*.py -v
```

Expected: all PASS

- [ ] **Step 4: Commit final docs**

```bash
git add playground/README.md docs/primeiros-passos.md
git commit -m "docs: README do playground e link em primeiros passos"
```

---

## Spec coverage checklist

| Spec item | Task |
| --- | --- |
| `playground/` autocontida | 1–7 |
| Compose 1+2 shards | 3 |
| Seed script `--apply` / `--dump-sql` | 2–3 |
| SQL versionado | 3 |
| config 2 shards + DuckDB | 4 |
| Streamlit chat + debug | 5–6 |
| Perguntas prontas + expected | 4, 6 |
| Extra `[playground]` | 6 |
| README + primeiros-passos | 7 |
| Sem mudar lib | (nenhuma task toca `txt2sql/`) |
| `.superpowers/` gitignore | já commitado na spec |

---

## Execution notes

- Rodar testes da raiz do repo para `playground.*` importar.
- Commits frequentes por task.
- Se Docker não estiver disponível na máquina do agent, Task 3 Step 4 pode ser pulado com nota no commit message / README.
