"""Extrai tool calls / SQL / guardrail das mensagens do turno."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger


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


_GUARDRAIL_MARKERS = (
    "guardrail",
    "não permitido",
    "nao permitido",
    "rejeit",
    "read-only",
    "apenas select",
    "roteador",
)

DEFAULT_TURN_LOG = Path(__file__).resolve().parent / "logs" / "turns.jsonl"


def extract_turn_debug(messages: list[Any]) -> TurnDebug:
    """Associa cada ToolMessage ao tool_call precedente pelo tool_call_id."""
    pending: dict[str, DebugStep] = {}
    steps: list[DebugStep] = []
    final = ""
    guardrail = False

    for msg in messages:
        cls_name = msg.__class__.__name__
        content = getattr(msg, "content", "") or ""
        if cls_name == "AIMessage":
            tool_calls = getattr(msg, "tool_calls", None) or []
            if tool_calls:
                for tc in tool_calls:
                    step = DebugStep(
                        name=tc.get("name", "?"),
                        args=dict(tc.get("args") or {}),
                    )
                    pending[tc.get("id", "")] = step
                    steps.append(step)
            elif content:
                final = content if isinstance(content, str) else str(content)
        elif cls_name == "ToolMessage":
            tid = getattr(msg, "tool_call_id", "")
            text = content if isinstance(content, str) else str(content)
            if tid in pending:
                pending[tid].result = text
            low = text.lower()
            if any(m in low for m in _GUARDRAIL_MARKERS):
                guardrail = True

    return TurnDebug(
        steps=steps,
        final_answer=final,
        looks_like_guardrail_reject=guardrail,
    )


def turn_debug_payload(
    debug: TurnDebug,
    *,
    question: str | None = None,
    thread_id: str | None = None,
    expected: str | None = None,
    expected_notes: str | None = None,
) -> dict[str, Any]:
    """Serializa o mesmo conteúdo do painel de debug da UI."""
    return {
        "ts": datetime.now(UTC).isoformat(),
        "thread_id": thread_id,
        "question": question,
        "expected": expected,
        "expected_notes": expected_notes,
        "looks_like_guardrail_reject": debug.looks_like_guardrail_reject,
        "steps": [asdict(s) for s in debug.steps],
        "final_answer": debug.final_answer,
    }


def log_turn_debug(
    debug: TurnDebug,
    *,
    question: str | None = None,
    thread_id: str | None = None,
    expected: str | None = None,
    expected_notes: str | None = None,
    jsonl_path: Path | None = DEFAULT_TURN_LOG,
) -> dict[str, Any]:
    """Grava o debug do turno no loguru e (opcional) em ``turns.jsonl``."""
    payload = turn_debug_payload(
        debug,
        question=question,
        thread_id=thread_id,
        expected=expected,
        expected_notes=expected_notes,
    )
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    logger.info("playground turn debug\n{}", body)

    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        logger.debug("turn debug append → {}", jsonl_path)

    return payload
