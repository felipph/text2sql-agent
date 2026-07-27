# Contributing

## Workflow de desenvolvimento

1. Crie uma branch a partir da default:

   ```bash
   git checkout -b feat/descricao-curta
   ```

2. Faça as mudanças com testes. Prefira TDD para comportamento novo.

3. Rode lint e testes:

   ```bash
   .venv/bin/ruff check .
   .venv/bin/ruff format .
   .venv/bin/pytest tests/ -v
   .venv/bin/python smoke_test.py
   ```

4. Abra um Pull Request descrevendo o *porquê* da mudança.

> Se o workspace ainda não tiver `.git`, inicialize o repositório antes de contribuir.

## Estilo de código

Automatizado via Ruff (`pyproject.toml`: `line-length = 100`, `target-version = py312`):

```bash
.venv/bin/ruff check .
.venv/bin/ruff format .
```

Não duplique regras do linter em reviews — rode o Ruff.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/) em inglês curto, foco no *why*:

- `feat(duckdb): stream materialize in batches`
- `fix(guardrail): reject nested DML in CTE`
- `docs: add Standard-tier documentation`
- `test: cover empty DuckDB materialize`

## Testes

* Unitários em `tests/` (pytest).
* Smokes `smoke_test.py` e `smoke_test_graph.py` devem passar antes do merge em mudanças de grafo/DB.
* Mudanças em `materialize` / guardrail / shard exigem cobertura de borda (vazio, idempotência, limites).

## Review

* API pública (`txt2sql/__init__.py`) só muda com justificativa e atualização de `docs/referencia/api.md`.
* Não adicionar fan-out entre shards.
* Docs em PT-BR; termos técnicos padrão em inglês.
* Specs de features grandes podem viver em `docs/superpowers/` sem substituir a doc de produto.
