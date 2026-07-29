"""Store de sessões DuckDB file-backed indexadas por thread_id."""

from __future__ import annotations

import re
from pathlib import Path

from txt2sql.db.duckdb_layer import DuckDBSession

_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


class DuckDBSessionStore:
    """Gerencia sessões DuckDB persistentes por thread_id."""

    def __init__(self, root_dir: Path) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, DuckDBSession] = {}

    def _safe_id(self, thread_id: str) -> str:
        return _SAFE_ID_RE.sub("_", thread_id)

    def get(self, thread_id: str) -> DuckDBSession:
        cached = self._sessions.get(thread_id)
        if cached is not None:
            return cached
        safe = self._safe_id(thread_id)
        db_path = self._root / f"{safe}.duckdb"
        session = DuckDBSession(database=str(db_path))
        self._sessions[thread_id] = session
        return session

    def close(self, thread_id: str) -> None:
        session = self._sessions.pop(thread_id, None)
        if session is not None:
            session.close()


__all__ = ["DuckDBSessionStore"]
