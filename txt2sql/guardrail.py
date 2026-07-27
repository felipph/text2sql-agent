"""Guardrail de somente-leitura para validação de SQL via AST do sqlglot.

A validação é *fail-closed*: qualquer situação ambígua, comando não reconhecido
ou erro de parse resulta em rejeição. As regras aplicadas:

* Apenas um único statement por query.
* Apenas ``SELECT`` (ou CTE ``WITH`` que termina em ``SELECT``) é permitido.
* Nenhuma expressão de DML/DDL/controle em *qualquer* profundidade da árvore
  (ex.: ``INSERT`` dentro de subquery, ``UPDATE`` em CTE, etc.).
* Denylist de comandos perigosos específicos de T-SQL / procedurais.
* Allowlist opcional de tabelas: quando fornecida, toda tabela referenciada
  deve pertencer ao escopo permitido (após ``optimizer.qualify``).
"""

from __future__ import annotations

from sqlglot import exp, parse
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from loguru import logger


class ReadOnlyViolationError(Exception):
    """Erro levantado quando uma query viola as regras de somente-leitura.

    O nome é mantido para compatibilidade com o tratamento de erros do
    LangChain (o agente captura este erro e devolve a mensagem ao LLM).
    """


# Expressões proibidas em qualquer profundidade da árvore sintática.
# Cobrem DML, DDL, controle transacional e comandos administrativos.
_FORBIDDEN_EXPRESSIONS: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,          # comandos genéricos não modelados (ex.: GRANT, EXEC)
    exp.Transaction,      # BEGIN / START TRANSACTION
    exp.Commit,
    exp.Rollback,
    exp.Set,              # SET ...
    exp.Use,              # USE <db>
)

# Denylist textual (case-insensitive) — palavras-chave perigosas de T-SQL e
# comandos procedurais que podem não virar um nó específico do sqlglot.
_DENYLIST_KEYWORDS: tuple[str, ...] = (
    "exec",
    "execute",
    "sp_",
    "xp_",
    "insert",
    "update",
    "delete",
    "merge",
    "drop",
    "create",
    "alter",
    "truncate",
    "grant",
    "revoke",
    "into",          # SELECT ... INTO cria tabela
    "openrowset",
    "openquery",
    "bulk",
    "shutdown",
    "waitfor",
    "reconfigure",
    "dbcc",
)


def _contains_denylisted_keyword(sql: str) -> str | None:
    """Retorna a primeira palavra da denylist encontrada no SQL (ou ``None``)."""
    lowered = sql.lower()
    for keyword in _DENYLIST_KEYWORDS:
        # Fronteira simples: prefixos como sp_/xp_ verificados por substring,
        # palavras verificadas com espaços/limites ao redor.
        if keyword.endswith("_"):
            if keyword in lowered:
                return keyword
        else:
            # verifica ocorrência como token isolado
            import re

            if re.search(rf"\b{re.escape(keyword)}\b", lowered):
                return keyword
    return None


def build_scope(allowed_tables: list[str] | None) -> set[str] | None:
    """Constrói o conjunto de nomes de tabela permitidos (normalizado).

    Args:
        allowed_tables: Lista de nomes de tabela lógicos/físicos permitidos.
            Pode incluir nomes qualificados (``schema.tabela``). Se ``None``,
            nenhuma restrição de tabela é aplicada.

    Returns:
        Conjunto normalizado (lower-case, apenas o nome final da tabela e
        também a forma qualificada) ou ``None`` se sem restrição.
    """
    if allowed_tables is None:
        return None
    scope: set[str] = set()
    for name in allowed_tables:
        norm = name.strip().lower()
        scope.add(norm)
        # também adiciona apenas o último componente (tabela sem schema)
        scope.add(norm.split(".")[-1])
    return scope


def _extract_table_names(tree: exp.Expression) -> set[str]:
    """Extrai todos os nomes de tabela referenciados na árvore (normalizados)."""
    names: set[str] = set()
    for table in tree.find_all(exp.Table):
        # ignora aliases de CTE (tratados por qualify) — pega o nome real
        parts = [p.name for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p]
        if table.name:
            names.add(table.name.lower())
            if parts:
                names.add(".".join(p.lower() for p in parts))
    return names


def validate_sql(
    sql: str,
    dialect: str | None = None,
    allowed_tables: list[str] | None = None,
) -> str:
    """Valida uma query SQL contra as regras de somente-leitura (fail-closed).

    Args:
        sql: A query SQL a validar.
        dialect: Dialeto do sqlglot para o parse (ex.: ``"tsql"``,
            ``"postgres"``). Se ``None``, usa o parser genérico.
        allowed_tables: Allowlist opcional de tabelas. Quando fornecida, toda
            tabela referenciada deve pertencer ao escopo.

    Returns:
        A própria query ``sql`` (inalterada) quando válida — conveniente para
        encadear com a execução.

    Raises:
        ReadOnlyViolationError: Se a query violar qualquer regra.
    """
    if not sql or not sql.strip():
        raise ReadOnlyViolationError("Query vazia não é permitida.")

    # 1) Denylist textual (barreira rápida antes do parse).
    hit = _contains_denylisted_keyword(sql)
    if hit is not None:
        # 'into' e alguns tokens podem ser falsos positivos apenas em contextos
        # válidos raros; mantemos fail-closed e rejeitamos.
        logger.warning("Guardrail: palavra proibida detectada: {}", hit)
        raise ReadOnlyViolationError(
            f"Comando/keyword proibido detectado: {hit!r}. Apenas SELECT é permitido."
        )

    # 2) Parse AST — falha de parse => rejeição.
    try:
        statements = parse(sql, dialect=dialect)
    except ParseError as err:
        raise ReadOnlyViolationError(f"SQL inválido (parse falhou): {err}") from err

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise ReadOnlyViolationError(
            f"Apenas um único statement é permitido (encontrados {len(statements)})."
        )

    tree = statements[0]

    # 3) O statement raiz deve ser SELECT (ou WITH ... SELECT).
    root = tree
    if isinstance(root, exp.With):
        root = root.this
    if not isinstance(tree, (exp.Select, exp.With, exp.Union)):
        raise ReadOnlyViolationError(
            f"Apenas SELECT é permitido; statement do tipo {type(tree).__name__} rejeitado."
        )

    # 4) Nenhuma expressão proibida em qualquer profundidade.
    for forbidden in _FORBIDDEN_EXPRESSIONS:
        node = tree.find(forbidden)
        if node is not None:
            logger.warning("Guardrail: expressão proibida na AST: {}", forbidden.__name__)
            raise ReadOnlyViolationError(
                f"Expressão proibida detectada na query: {forbidden.__name__}."
            )

    # 5) Allowlist de tabelas (após qualify para resolver aliases/CTEs).
    scope = build_scope(allowed_tables)
    if scope is not None:
        try:
            qualified = qualify(tree.copy(), dialect=dialect)
        except OptimizeError:
            # qualify pode falhar sem schema; caímos para a árvore original.
            qualified = tree

        referenced = _extract_table_names(qualified)
        # CTEs definem nomes locais que não precisam estar no escopo.
        cte_names = {c.alias_or_name.lower() for c in qualified.find_all(exp.CTE)}
        offending = {
            name for name in referenced if name not in scope and name not in cte_names
        }
        if offending:
            logger.warning("Guardrail: tabelas fora do escopo: {}", offending)
            raise ReadOnlyViolationError(
                f"Tabela(s) fora do escopo permitido: {sorted(offending)}. "
                f"Permitidas: {sorted(scope)}."
            )

    logger.debug("Guardrail: query aprovada.")
    return sql
