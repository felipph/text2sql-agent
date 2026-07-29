"""Testes do helper de tracing Langfuse (sem rede)."""

from __future__ import annotations

from typing import Any

from txt2sql.tracing import (
    build_tracing_callbacks,
    build_tracing_run_config,
    flush_tracing_callbacks,
    is_tracing_enabled,
)


def test_tracing_disabled_without_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert is_tracing_enabled() is False
    assert build_tracing_callbacks() == []
    assert build_tracing_run_config(session_id="t1") == {}


def test_tracing_enabled_flag(monkeypatch: Any) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    assert is_tracing_enabled() is True


def test_build_callbacks_without_package_returns_empty(monkeypatch: Any) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr("txt2sql.tracing._import_callback_handler", lambda: None)
    assert build_tracing_callbacks(session_id="t1", tags=["playground"]) == []


def test_build_tracing_run_config_metadata(monkeypatch: Any) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    class FakeHandlerV4:
        """Assinatura no estilo Langfuse v4 (sem secret_key)."""

        def __init__(
            self,
            *,
            public_key: str | None = None,
            trace_context: Any = None,
        ) -> None:
            self.public_key = public_key
            self.trace_context = trace_context

    ensured: list[bool] = []

    monkeypatch.setattr("txt2sql.tracing._import_callback_handler", lambda: FakeHandlerV4)
    monkeypatch.setattr(
        "txt2sql.tracing._ensure_langfuse_client",
        lambda: ensured.append(True),
    )
    cfg = build_tracing_run_config(
        session_id="thread-1",
        trace_name="txt2sql-playground",
        tags=["playground"],
        metadata={"app": "playground"},
    )
    assert ensured == [True]
    assert len(cfg["callbacks"]) == 1
    assert cfg["callbacks"][0].public_key is None
    meta = cfg["metadata"]
    assert meta["langfuse_session_id"] == "thread-1"
    assert meta["langfuse_tags"] == ["playground"]
    assert meta["langfuse_trace_name"] == "txt2sql-playground"
    assert meta["app"] == "playground"


def test_build_callbacks_v2_passes_credentials(monkeypatch: Any) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://langfuse.local")

    class FakeHandlerV2:
        def __init__(
            self,
            *,
            public_key: str,
            secret_key: str,
            host: str = "",
            session_id: str | None = None,
            **kwargs: Any,
        ) -> None:
            self.kwargs = {
                "public_key": public_key,
                "secret_key": secret_key,
                "host": host,
                "session_id": session_id,
                **kwargs,
            }

    monkeypatch.setattr("txt2sql.tracing._import_callback_handler", lambda: FakeHandlerV2)
    cbs = build_tracing_callbacks(session_id="t1", tags=["playground"])
    assert len(cbs) == 1
    assert cbs[0].kwargs["public_key"] == "pk-test"
    assert cbs[0].kwargs["secret_key"] == "sk-test"
    assert cbs[0].kwargs["host"] == "http://langfuse.local"
    assert cbs[0].kwargs["session_id"] == "t1"


def test_flush_tracing_callbacks_tolerates_errors() -> None:
    class Boom:
        def flush(self) -> None:
            raise RuntimeError("boom")

    flush_tracing_callbacks([Boom(), object()])
