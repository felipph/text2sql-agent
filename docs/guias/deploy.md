# Deploy

Como liberar e consumir a biblioteca. Não há serviço HTTP próprio.

## Ambientes

| Ambiente | Uso |
|----------|-----|
| Local / CI | `pip install -e ".[dev]"` ou `uv sync` + smokes |
| App consumidora | `pip install` do pacote (ou path/editable) + YAML + env vars |
| Produção | A app hospedeira deploya; `txt2sql` é dependência de biblioteca |

## Processo de release

1. Atualize `version` em `pyproject.toml` e `__version__` em `txt2sql/__init__.py`.
2. Rode a verificação:

   ```bash
   .venv/bin/ruff check .
   .venv/bin/pytest tests/ -v
   .venv/bin/python smoke_test.py
   .venv/bin/python smoke_test_graph.py
   ```

3. Publique o artefato no índice interno/PyPI da organização (comando depende do registry — <!-- TODO: documentar o registry real quando existir -->).

4. Na app consumidora, pin a versão e redeploy.

## Checklist pré-release

- [ ] Versão alinhada em `pyproject.toml` e `__init__.py`
- [ ] README / `docs/referencia/api.md` atualizados se a API pública mudou
- [ ] ADRs novos para decisões relevantes
- [ ] Smokes verdes
- [ ] Sem secrets em examples/ ou docs

## Rollback

1. Na app consumidora, reverta o pin da dependência para a versão anterior conhecida.
2. Redeploy da app.
3. Se o problema for config YAML (não código), reverta o YAML/env sem mudar o pacote.

Procedimento operacional durante incidente: veja [runbook](runbook.md#rollback).
