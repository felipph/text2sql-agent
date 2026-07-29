# Suite adversária do Guardrail (`validate_sql`)

## Contexto

`txt2sql/guardrail.py` valida SQL read-only de forma fail-closed (denylist textual + AST sqlglot + allowlist opcional de tabelas). Hoje a cobertura está no `smoke_test.py` e indiretamente no Policy Gate — não há suite pytest dedicada a `validate_sql`, nem caça sistemática a bypasses.

## Objetivo

Montar testes inventivos focados em **quebrar** o Guardrail: payloads que *deveriam* ser rejeitados. Nesta fase **apenas revelar** buracos (falsos negativos); **não** alterar `guardrail.py`.

## Decisões

| Tema | Escolha |
|------|---------|
| Correção do guardrail | Não nesta fase (só revelar) |
| Buraco no pytest | Falha vermelha (`assert raises` e o SQL passa → FAIL) |
| Escopo | Máximo inventivo: vários dialetos + ofuscação |
| Organização | Duas camadas (regressão verde + break vermelho) |

## Arquitetura

### `tests/test_guardrail.py` — regressão (CI verde)

Cobertura estável do contrato atual:

- **Aprova:** `SELECT` simples, `WITH … SELECT`, `UNION` de SELECTs, allowlist com tabela no escopo, CTE local sem exigir nome no escopo.
- **Rejeita:** query vazia/whitespace; DML/DDL top-level; multi-statement clássico; keywords óbvias da denylist (`INSERT`, `EXEC`, `INTO`, …); tabela fora de `allowed_tables`.

Espelha o que o smoke já cobre, em pytest. Não substitui o `smoke_test.py`.

### `tests/test_guardrail_break.py` — red team (pode ficar vermelho)

Cada caso:

```python
with pytest.raises(ReadOnlyViolationError):
    validate_sql(sql, dialect=dialect, allowed_tables=allowed_tables)
```

- Verde = ataque bloqueado.
- Vermelho = `validate_sql` devolveu o SQL → **buraco**, identificado pelo `case_id`.

Payloads via `@pytest.mark.parametrize("case_id, sql, dialect, allowed_tables", [...])`.

Controles positivos (SELECT inocente) ficam **somente** em `test_guardrail.py`.

## Categorias de ataque (break)

1. **DML/DDL aninhado** — CTE/subquery com `DELETE`/`UPDATE`/`INSERT`/`MERGE` (+ `RETURNING` quando aplicável).
2. **Multi-statement / stacking** — segundo comando após `;`, inclusive com whitespace/comentário.
3. **Comment / token smuggling** — keywords partidas (`INS/**/ERT`, `INT/**/O`, etc.).
4. **Ofuscação** — unicode lookalikes, zero-width, null byte, newlines/tabs mid-keyword.
5. **SELECT INTO / materialização** — variantes de `INTO` (já na denylist) com comentários/aliases.
6. **T-SQL procedural** — `EXEC`/`EXECUTE`, `xp_`/`sp_`, `OPENROWSET`/`OPENQUERY`, `WAITFOR`, `DBCC`, `BULK`.
7. **Postgres side-effects** — `COPY … PROGRAM`, `pg_read_file`, `lo_import`, dblink, `SELECT … INTO` temp.
8. **Controle de sessão** — `SET`, `USE`, `BEGIN`/`COMMIT` sozinhos ou prefixando SELECT.
9. **Allowlist bypass** — schema/catalog, alias vs nome real, CTE homônima de tabela fora do escopo.
10. **Parse ambiguity** — nó `Command` genérico; qualquer parse fail deve rejeitar (se aprovar = buraco).
11. **UNION / compostos** — braço DML se o dialeto permitir (UNION inocente fica na regressão).

Cada categoria: ~3–8 payloads. Dialetos típicos: `None`/genérico, `postgres`, `tsql` (e outros quando o payload exigir).

## Fora de escopo

- Alterar `txt2sql/guardrail.py` (fixes ficam para fase posterior).
- Policy Gate / volume / `force_analytical`.
- Listener do engine em `DatabaseRegistry`.
- Substituir ou remover o smoke de guardrail.

## Como rodar / interpretar

```bash
.venv/bin/pytest tests/test_guardrail.py -v          # deve ficar verde
.venv/bin/pytest tests/test_guardrail_break.py -v    # inventário de buracos
```

Falha em `test_guardrail_break` com mensagem do tipo “DID NOT RAISE” = buraco documentado pelo `case_id`. Nesta fase isso é resultado esperado, não regressão a “corrigir” nos testes (não usar `xfail`).

## Entrega

- `tests/test_guardrail.py`
- `tests/test_guardrail_break.py`
- Esta spec

Sem mudanças em código de produção.
