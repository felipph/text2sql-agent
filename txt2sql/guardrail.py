"""Guardrail de somente-leitura para validação de SQL via AST do sqlglot.

A validação é *fail-closed*: qualquer situação ambígua, comando não reconhecido
ou erro de parse resulta em rejeição. As regras aplicadas:

* Apenas um único statement por query.
* Apenas ``SELECT`` (ou CTE ``WITH`` que termina em ``SELECT``) é permitido;
  ``EXPLAIN`` de um SELECT interno também (sem ``ANALYZE``).
* Nenhuma expressão de DML/DDL/controle em *qualquer* profundidade da árvore
  (ex.: ``INSERT`` dentro de subquery, ``UPDATE`` em CTE, etc.).
* Denylist de comandos perigosos específicos de T-SQL / procedurais
  (avaliada nos tokens, ignorando comentários).
* Denylist de funções com efeito colateral (ex.: ``pg_terminate_backend``,
  ``dblink``, ``lo_import``).
* Allowlist opcional de tabelas: quando fornecida, toda tabela referenciada
  deve pertencer ao escopo permitido (após ``optimizer.qualify``); identificadores
  entre aspas são comparados de forma case-sensitive.
"""

from __future__ import annotations

import re

from loguru import logger
from sqlglot import exp, parse, tokenize
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.qualify import qualify
from sqlglot.tokens import TokenType


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
    exp.Command,  # comandos genéricos não modelados (ex.: GRANT, EXEC)
    exp.Transaction,  # BEGIN / START TRANSACTION
    exp.Commit,
    exp.Rollback,
    exp.Set,  # SET ...
    exp.Use,  # USE <db>
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
    "into",  # SELECT ... INTO cria tabela
    "openrowset",
    "openquery",
    "bulk",
    "shutdown",
    "waitfor",
    "reconfigure",
    "dbcc",
)

# Funções / pacotes com efeito colateral mesmo dentro de SELECT.
_FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {
        # Postgres / extensões
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "lo_import",
        "lo_export",
        "lo_unlink",
        "setval",
        "nextval",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_connect_u",
        "dblink_send_query",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_write_file",
        "pg_ls_dir",
        "query_to_xml",
        "sys_exec",
        "sys_eval",
        # Oracle
        "utl_http",
        "utl_file",
        "utl_tcp",
        "utl_smtp",
        "utl_inaddr",
        "dbms_java",
        "dbms_scheduler",
        # SQL Server (redundante com xp_/sp_, mas cobre chamada direta)
        "xp_cmdshell",
        "openrowset",
        "openquery",
        "opendas",
    }
)

# Tokens cujo texto não deve alimentar a denylist (literais).
_DENYLIST_SKIP_TOKEN_TYPES: frozenset[TokenType] = frozenset(
    {
        TokenType.STRING,
        TokenType.NUMBER,
        TokenType.BIT_STRING,
        TokenType.HEX_STRING,
        TokenType.BYTE_STRING,
        TokenType.NATIONAL_STRING,
        TokenType.RAW_STRING,
        TokenType.HEREDOC_STRING,
    }
)


def _contains_denylisted_keyword(sql: str, dialect: str | None = None) -> str | None:
    """Retorna a primeira palavra da denylist encontrada no SQL (ou ``None``).

    Comentários são ignorados (sqlglot anexa-os aos tokens vizinhos). Literais
    de string/número também são ignorados para evitar falso positivo em
    ``SELECT 'delete'``.
    """
    try:
        tokens = tokenize(sql, dialect=dialect)
    except TokenError:
        # Tokenização falhou: cai no scan textual bruto (fail-closed).
        tokens = None

    if tokens is not None:
        parts: list[str] = []
        for token in tokens:
            if token.token_type in _DENYLIST_SKIP_TOKEN_TYPES:
                continue
            parts.append(token.text)
        haystack = " ".join(parts).lower()
    else:
        haystack = sql.lower()

    for keyword in _DENYLIST_KEYWORDS:
        if keyword.endswith("_"):
            if keyword in haystack:
                return keyword
        elif re.search(rf"\b{re.escape(keyword)}\b", haystack):
            return keyword
    return None


def _function_name(node: exp.Expression) -> str | None:
    """Extrai o nome de uma chamada de função, se houver."""
    if isinstance(node, exp.Anonymous):
        return (node.name or "").lower() or None
    if isinstance(node, exp.Func):
        name = getattr(node, "name", None) or ""
        return name.lower() or None
    return None


def _forbidden_function_hit(tree: exp.Expression) -> str | None:
    """Retorna o nome da função proibida encontrada na árvore, se houver."""
    for node in tree.find_all(exp.Func):
        name = _function_name(node)
        if name and name in _FORBIDDEN_FUNCTIONS:
            return name

    # Pacotes Oracle / schema.func (ex.: UTL_HTTP.REQUEST)
    for dot in tree.find_all(exp.Dot):
        left = dot.this
        if isinstance(left, exp.Identifier):
            pkg = left.name.lower()
            if pkg in _FORBIDDEN_FUNCTIONS:
                return pkg
        # Também rejeita se o SQL do Dot começa com pacote proibido.
        rendered = dot.sql().lower()
        for forbidden in _FORBIDDEN_FUNCTIONS:
            if rendered.startswith(f"{forbidden}."):
                return forbidden

    # FROM dblink(...) AS t — Anonymous como "nome" de tabela
    for table in tree.find_all(exp.Table):
        if isinstance(table.this, exp.Anonymous):
            name = _function_name(table.this)
            if name and name in _FORBIDDEN_FUNCTIONS:
                return name
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


def _build_exact_scope(allowed_tables: list[str]) -> set[str]:
    """Escopo case-sensitive para identificadores entre aspas."""
    scope: set[str] = set()
    for name in allowed_tables:
        raw = name.strip()
        scope.add(raw)
        scope.add(raw.split(".")[-1])
    return scope


def _allowlist_offenders(
    tree: exp.Expression,
    scope_lower: set[str],
    scope_exact: set[str],
) -> set[str]:
    """Tabelas / fontes fora do escopo (respeita aspas case-sensitive)."""
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    offending: set[str] = set()

    for table in tree.find_all(exp.Table):
        ident = table.this

        # Função-tabela / fonte sem identificador simples: fail-closed.
        if ident is not None and not isinstance(ident, exp.Identifier):
            if isinstance(ident, exp.Anonymous):
                fname = _function_name(ident) or "function"
                offending.add(fname)
            continue

        if not table.name:
            continue

        # CTE local (qualify pode citar o alias com aspas).
        if table.name.lower() in cte_names:
            continue

        catalog = table.args.get("catalog")
        db = table.args.get("db")
        has_schema = catalog is not None or db is not None
        parts = [
            p.name
            for p in (catalog, db, ident)
            if p is not None and hasattr(p, "name")
        ]
        qualified = ".".join(parts) if parts else table.name
        quoted = bool(getattr(ident, "quoted", False)) if ident is not None else False

        if quoted:
            # Identificador quoted: match exato (case-sensitive) na allowlist.
            candidates = {table.name, qualified}
            if has_schema:
                # Schema qualificado: exige a forma completa na allowlist.
                if qualified not in scope_exact:
                    offending.add(qualified)
            elif not candidates.intersection(scope_exact):
                offending.add(table.name)
            continue

        name_lower = table.name.lower()
        qualified_lower = qualified.lower()
        if has_schema:
            # `other_schema.users` não passa só porque `users` está na allowlist.
            if qualified_lower not in scope_lower:
                offending.add(qualified_lower)
        elif name_lower not in scope_lower:
            offending.add(name_lower)

    return offending


def _unwrap_explain(
    tree: exp.Expression,
    dialect: str | None,
    allowed_tables: list[str] | None,
    original_sql: str,
) -> str | None:
    """Se ``tree`` for EXPLAIN de um SELECT, valida o SQL interno e devolve o original.

    Retorna ``None`` se não for um EXPLAIN tratável (caller segue o fluxo normal).
    """
    if not isinstance(tree, exp.Command):
        return None
    if str(tree.this).upper() != "EXPLAIN":
        return None

    inner = tree.expression
    if not isinstance(inner, exp.Literal) or not inner.is_string:
        raise ReadOnlyViolationError(
            "EXPLAIN sem SELECT interno validável foi rejeitado."
        )

    inner_sql = str(inner.this).strip()
    # Rejeita EXPLAIN ANALYZE / opções que alteram o prefixo do SELECT.
    if not re.match(r"^(WITH|SELECT)\b", inner_sql, flags=re.IGNORECASE):
        raise ReadOnlyViolationError(
            "Apenas EXPLAIN de SELECT/WITH é permitido (ANALYZE/opções rejeitadas)."
        )

    validate_sql(inner_sql, dialect=dialect, allowed_tables=allowed_tables)
    logger.debug("Guardrail: EXPLAIN de SELECT aprovado.")
    return original_sql


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

    # 1) Denylist textual (barreira rápida antes do parse; ignora comentários).
    hit = _contains_denylisted_keyword(sql, dialect=dialect)
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

    # 2b) EXPLAIN SELECT → valida o SELECT interno (fail-closed se não for SELECT).
    explained = _unwrap_explain(tree, dialect, allowed_tables, sql)
    if explained is not None:
        return explained

    # 3) O statement raiz deve ser SELECT (ou WITH ... SELECT).
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

    # 4b) Funções com efeito colateral (mesmo em SELECT aparentemente inocente).
    fn_hit = _forbidden_function_hit(tree)
    if fn_hit is not None:
        logger.warning("Guardrail: função proibida na AST: {}", fn_hit)
        raise ReadOnlyViolationError(
            f"Função proibida detectada na query: {fn_hit!r}."
        )

    # 5) Allowlist de tabelas (após qualify para resolver aliases/CTEs).
    scope = build_scope(allowed_tables)
    if scope is not None:
        assert allowed_tables is not None
        try:
            qualified = qualify(tree.copy(), dialect=dialect)
        except OptimizeError:
            # qualify pode falhar sem schema; caímos para a árvore original.
            qualified = tree

        scope_exact = _build_exact_scope(allowed_tables)
        offending = _allowlist_offenders(qualified, scope, scope_exact)
        if offending:
            logger.warning("Guardrail: tabelas fora do escopo: {}", offending)
            raise ReadOnlyViolationError(
                f"Tabela(s) fora do escopo permitido: {sorted(offending)}. "
                f"Permitidas: {sorted(scope)}."
            )

    logger.debug("Guardrail: query aprovada.")
    return sql
