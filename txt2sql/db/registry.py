"""Registro de múltiplas conexões de banco de dados por ID.

O :class:`DatabaseRegistry` cria e mantém um engine SQLAlchemy por banco
declarado na configuração, instalando um *guardrail listener* de somente-leitura
em cada engine marcado como ``read_only``, aplicando timeouts de conexão e,
opcionalmente, deadline de execução de queries SELECT no cliente.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any

from loguru import logger
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import make_url

from txt2sql.config import AgentConfig, DatabaseConfig
from txt2sql.guardrail import ReadOnlyViolationError, validate_sql


class QueryTimeoutError(Exception):
    """Query SELECT excedeu o ``query_timeout`` configurado."""

    def __init__(self, database_id: str, timeout: int) -> None:
        self.database_id = database_id
        self.timeout = timeout
        super().__init__(
            f"Query no banco {database_id!r} excedeu o timeout de {timeout}s"
        )


class DatabaseRegistry:
    """Mantém e fornece engines/conexões SQLAlchemy indexados por ``database_id``.

    Args:
        config: Configuração do agente (fonte dos bancos declarados).
    """

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._engines: dict[str, Engine] = {}
        # engines de introspecção — SEM guardrail; usados apenas internamente
        # para reflection/discovery de schema (queries controladas pela lib,
        # ex.: PRAGMA/catálogo do sistema, que o guardrail read-only rejeitaria).
        self._inspection_engines: dict[str, Engine] = {}
        self._build_engines()

    # ------------------------------------------------------------------ #
    # Construção
    # ------------------------------------------------------------------ #
    def _build_engines(self) -> None:
        """Instancia um engine por banco declarado, com timeouts e guardrail."""
        for db in self._config.databases:
            engine = self._create_engine(db)
            if db.read_only:
                self._install_readonly_guardrail(engine, db)
            self._engines[db.id] = engine
            # engine de introspecção (sem listener de guardrail)
            self._inspection_engines[db.id] = self._create_engine(db)
            logger.info(
                "Engine criado para banco {!r} (read_only={})", db.id, db.read_only
            )

    def _create_engine(self, db: DatabaseConfig) -> Engine:
        """Cria um engine SQLAlchemy para um banco, aplicando timeout.

        A chave do timeout de conexão varia por driver: PostgreSQL/MySQL usam
        ``connect_timeout`` (segundos), SQLite usa ``timeout``. Drivers cujo
        nome não é reconhecido recebem o engine sem ``connect_args`` de timeout.
        """
        conn_str = db.resolve_connection_string(self._config.override_connections)
        connect_args = self._timeout_connect_args(conn_str, db.connect_timeout)
        return create_engine(
            conn_str,
            connect_args=connect_args,
            pool_pre_ping=True,
        )

    @staticmethod
    def _timeout_connect_args(conn_str: str, connect_timeout: int) -> dict[str, Any]:
        """Constrói ``connect_args`` de timeout apropriado ao backend."""
        if not connect_timeout:
            return {}
        backend = make_url(conn_str).get_backend_name().lower()
        if backend in ("postgresql", "mysql", "mariadb", "mssql"):
            return {"connect_timeout": connect_timeout}
        if backend == "sqlite":
            return {"timeout": connect_timeout}
        # backend desconhecido: não arrisca argumento incompatível
        return {}

    def _install_readonly_guardrail(self, engine: Engine, db: DatabaseConfig) -> None:
        """Instala listener que valida cada SQL executado no engine.

        Usa o dialeto do engine para o parse do sqlglot. Qualquer statement que
        não passe em :func:`validate_sql` levanta :class:`ReadOnlyViolationError`
        antes de tocar o banco (fail-closed).
        """
        dialect_name = self._sqlglot_dialect(engine)

        @event.listens_for(engine, "before_cursor_execute")
        def _before_cursor_execute(
            conn, cursor, statement, parameters, context, executemany
        ):
            # Ignora pings internos do pool e comandos vazios.
            stmt = (statement or "").strip()
            if not stmt:
                return
            # Permite apenas SELECT/WITH; validate_sql é fail-closed.
            validate_sql(stmt, dialect=dialect_name)

        logger.debug(
            "Guardrail read-only instalado no engine {!r} (dialeto sqlglot={})",
            db.id,
            dialect_name,
        )

    @staticmethod
    def _sqlglot_dialect(engine: Engine) -> str | None:
        """Mapeia o dialeto SQLAlchemy para o nome de dialeto do sqlglot."""
        name = engine.dialect.name.lower()
        mapping = {
            "postgresql": "postgres",
            "mssql": "tsql",
            "mysql": "mysql",
            "sqlite": "sqlite",
            "oracle": "oracle",
            "snowflake": "snowflake",
            "duckdb": "duckdb",
        }
        return mapping.get(name)

    # ------------------------------------------------------------------ #
    # API pública
    # ------------------------------------------------------------------ #
    def get_engine(self, database_id: str) -> Engine:
        """Retorna o engine SQLAlchemy de um banco.

        Args:
            database_id: ID do banco.

        Returns:
            O :class:`~sqlalchemy.Engine` correspondente.

        Raises:
            KeyError: Se o banco não estiver registrado.
        """
        if database_id not in self._engines:
            raise KeyError(f"database_id não registrado: {database_id!r}")
        return self._engines[database_id]

    def get_inspection_engine(self, database_id: str) -> Engine:
        """Retorna o engine de introspecção (sem guardrail) de um banco.

        Uso exclusivamente interno para reflection/discovery de schema, onde as
        queries são geradas pela própria lib (não pelo LLM/usuário).
        """
        if database_id not in self._inspection_engines:
            raise KeyError(f"database_id não registrado: {database_id!r}")
        return self._inspection_engines[database_id]

    def get_connection(self, database_id: str) -> Connection:
        """Abre e retorna uma nova conexão para o banco indicado.

        O chamador é responsável por fechar a conexão (use como context manager).
        """
        return self.get_engine(database_id).connect()

    def dialect_of(self, database_id: str) -> str | None:
        """Retorna o nome do dialeto sqlglot para um banco."""
        return self._sqlglot_dialect(self.get_engine(database_id))

    def has_database(self, database_id: str) -> bool:
        """Indica se o banco está registrado."""
        return database_id in self._engines

    def execute(self, database_id: str, sql: str) -> list[dict[str, Any]]:
        """Executa uma query e retorna as linhas como lista de dicts.

        Respeita ``AgentConfig.effective_query_timeout``: se > 0, aplica
        deadline no cliente (thread + join). Estouro →
        :class:`QueryTimeoutError` após cancel/invalidate best-effort.

        A validação de somente-leitura já ocorre no listener do engine quando
        ``read_only=True``; aqui apenas executamos e materializamos o resultado.

        Args:
            database_id: Banco alvo.
            sql: Query SELECT a executar.

        Returns:
            Lista de linhas como dicionários ``{coluna: valor}``.
        """
        timeout = self._config.effective_query_timeout(database_id)
        engine = self.get_engine(database_id)

        if timeout == 0:
            with engine.connect() as conn:
                return self._fetch_dicts(conn, sql)

        conn = engine.connect()
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(self._fetch_dicts, conn, sql)
            try:
                return future.result(timeout=timeout)
            except FuturesTimeout as err:
                self._cancel_connection(conn)
                raise QueryTimeoutError(database_id, timeout) from err
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            try:
                conn.close()
            except Exception:  # noqa: BLE001, S110
                pass

    @staticmethod
    def _fetch_dicts(conn: Connection, sql: str) -> list[dict[str, Any]]:
        result = conn.execute(text(sql))
        columns = list(result.keys())
        return [dict(zip(columns, row)) for row in result.fetchall()]

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

    def dispose_all(self) -> None:
        """Descarta todos os pools de conexão (encerramento gracioso)."""
        for db_id, engine in self._engines.items():
            engine.dispose()
            logger.debug("Engine {!r} descartado", db_id)
        for engine in self._inspection_engines.values():
            engine.dispose()


__all__ = ["DatabaseRegistry", "QueryTimeoutError", "ReadOnlyViolationError"]
