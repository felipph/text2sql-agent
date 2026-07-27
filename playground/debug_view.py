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


_GUARDRAIL_MARKERS = (
    "guardrail",
    "não permitido",
    "nao permitido",
    "rejeit",
    "read-only",
    "apenas select",
)


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
