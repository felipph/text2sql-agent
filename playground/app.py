"""UI Streamlit: chat + painel de debug do agente txt2sql.

Execute a partir da raiz do repositório:

    streamlit run playground/app.py
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import streamlit as st
import yaml
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine, text

from playground.debug_view import TurnDebug, extract_turn_debug, log_turn_debug
from txt2sql import build_agent, load_config

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
PROMPTS_PATH = ROOT / "prompts.yaml"

DB_ENVS = {
    "db_main": "MAIN_DB_URL",
    "db_shard_1": "SHARD_1_DB_URL",
    "db_shard_2": "SHARD_2_DB_URL",
}


def _load_prompts() -> list[dict[str, Any]]:
    data = yaml.safe_load(PROMPTS_PATH.read_text(encoding="utf-8")) or {}
    return list(data.get("prompts") or [])


def ping_db(url: str) -> tuple[bool, str]:
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 — UI precisa mostrar qualquer falha
        return False, str(exc)


@st.cache_resource
def get_agent():
    config = load_config(str(CONFIG_PATH))
    return build_agent(config, checkpointer=MemorySaver())


def _chat_messages(messages: list[Any]) -> list[Any]:
    out: list[Any] = []
    for msg in messages:
        if isinstance(msg, (HumanMessage, AIMessage)) and not getattr(msg, "tool_calls", None):
            content = getattr(msg, "content", "") or ""
            if content:
                out.append(msg)
    return out


def _init_state() -> None:
    if "thread_id" not in st.session_state:
        st.session_state.thread_id = str(uuid.uuid4())
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "last_debug" not in st.session_state:
        st.session_state.last_debug = TurnDebug()
    if "expected" not in st.session_state:
        st.session_state.expected = None
    if "expected_notes" not in st.session_state:
        st.session_state.expected_notes = None
    if "pending_question" not in st.session_state:
        st.session_state.pending_question = None


def _run_turn(agent: Any, question: str) -> None:
    st.session_state.messages.append(HumanMessage(content=question))
    result = agent.invoke(
        {"messages": [HumanMessage(content=question)]},
        config={"configurable": {"thread_id": st.session_state.thread_id}},
    )
    all_msgs = list(result.get("messages") or [])
    st.session_state.messages = all_msgs
    debug = extract_turn_debug(all_msgs)
    st.session_state.last_debug = debug
    log_turn_debug(
        debug,
        question=question,
        thread_id=st.session_state.thread_id,
        expected=st.session_state.get("expected"),
        expected_notes=st.session_state.get("expected_notes"),
    )


def main() -> None:
    st.set_page_config(page_title="txt2sql playground", layout="wide")
    st.title("txt2sql playground")
    st.caption("Chat + debug de tools / SQL / shards · Postgres local via docker-compose")
    _init_state()

    with st.sidebar:
        st.subheader("Bancos")
        all_ok = True
        for label, env_name in DB_ENVS.items():
            url = os.environ.get(env_name)
            if not url:
                st.error(f"{label}: env `{env_name}` ausente")
                all_ok = False
                continue
            ok, detail = ping_db(url)
            if ok:
                st.success(f"{label}: up")
            else:
                st.error(f"{label}: {detail}")
                all_ok = False

        st.divider()
        st.text(f"YAML: {CONFIG_PATH.name}")
        st.code(st.session_state.thread_id, language=None)
        if st.button("Nova conversa"):
            st.session_state.thread_id = str(uuid.uuid4())
            st.session_state.messages = []
            st.session_state.last_debug = TurnDebug()
            st.session_state.expected = None
            st.session_state.expected_notes = None
            st.rerun()

        st.subheader("Perguntas prontas")
        for prompt in _load_prompts():
            if st.button(prompt["label"], key=f"prompt_{prompt['id']}"):
                st.session_state.pending_question = prompt["question"]
                st.session_state.expected = prompt.get("expected")
                st.session_state.expected_notes = prompt.get("notes")

    col_chat, col_debug = st.columns([1.4, 1.0])

    with col_chat:
        st.subheader("Chat")
        for msg in _chat_messages(st.session_state.messages):
            role = "user" if isinstance(msg, HumanMessage) else "assistant"
            with st.chat_message(role):
                st.markdown(msg.content)

        question = st.session_state.pop("pending_question", None)
        typed = st.chat_input(
            "Pergunte em linguagem natural…",
            disabled=not all_ok,
        )
        if typed:
            question = typed

        if question:
            if not all_ok:
                st.warning("Corrija a conexão com os bancos antes de perguntar.")
            else:
                try:
                    agent = get_agent()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Falha ao construir o agente (verifique AZURE_OPENAI_*): {exc}")
                else:
                    with st.spinner("Agente pensando…"):
                        _run_turn(agent, question)
                    st.rerun()

    with col_debug:
        st.subheader("Debug do turno")
        debug: TurnDebug = st.session_state.last_debug
        if st.session_state.expected:
            st.info(f"**Expected:** {st.session_state.expected}")
            if st.session_state.expected_notes:
                st.caption(st.session_state.expected_notes)
        if debug.looks_like_guardrail_reject:
            st.warning("Possível rejeição de guardrail detectada.")
        if not debug.steps and not debug.final_answer:
            st.caption("Nenhuma tool call ainda.")
        for i, step in enumerate(debug.steps, start=1):
            with st.expander(f"{i}. {step.name}", expanded=True):
                if step.args:
                    st.json(step.args)
                if step.result:
                    st.code(step.result, language="json")
        if debug.final_answer:
            st.markdown(f"**Resposta final:** {debug.final_answer}")


if __name__ == "__main__":
    main()
