"""Ask Your CRM — Streamlit chat UI (ChatGPT-style, single input box).

Run with:  streamlit run app.py
"""

from __future__ import annotations

import html
import re
import time
from datetime import datetime

import streamlit as st

from agent import FIXED_GREETING, active_model, ask_crm_events
from database import db_stats, init_db

st.set_page_config(page_title="Ask Your CRM — AI CRM Agent", page_icon="🤖", layout="wide")

# Create + seed the database on first load (no-op when it already exists).
if "db_ready" not in st.session_state:
    init_db()
    st.session_state.db_ready = True


# ---------------------------------------------------------------------------
# Styles — ChatGPT-like bubbles, single narrow chat column
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
    .block-container { padding-top: 1.0rem; max-width: 940px; margin: 0 auto; }
    header[data-testid="stHeader"] { background: transparent; }

    /* ---- message cards ---- */
    div[data-testid="stChatMessage"] {
        background: #ffffff;
        color: #1f2937;
        border: 1px solid #ececf1;
        border-radius: 16px;
        padding: 14px 18px;
        margin: 10px 0;
        box-shadow: 0 1px 3px rgba(16, 24, 40, .05);
    }
    div[data-testid="stChatMessage"] .stMarkdown { width: 100%; color: #1f2937; }
    /* keep wide markdown tables inside the chat column (no page scrollbar) */
    div[data-testid="stChatMessage"] .stMarkdown table {
        display: block; overflow-x: auto; max-width: 100%;
        border-collapse: collapse;
    }
    div[data-testid="stChatMessage"] .stMarkdown th,
    div[data-testid="stChatMessage"] .stMarkdown td { white-space: nowrap; }

    /* welcome card is tinted */
    div[data-testid="stChatMessage"]:has(.welcome-badge) {
        background: linear-gradient(135deg, #f8faff 0%, #f5f3ff 100%);
        border-color: #e0e7ff;
    }
    .welcome-badge {
        display: inline-block; font-size: .78rem; font-weight: 700;
        letter-spacing: .04em; color: #4f46e5; text-transform: uppercase;
        margin-bottom: 2px;
    }
    div[data-testid="stChatMessage"]:has(.welcome-badge) .stMarkdown { color: #1f2937; }

    /* ---- user bubble (right aligned, gradient) ---- */
    div[data-testid="stChatMessage"]:has(.user-bubble) {
        background: transparent; border: none; box-shadow: none;
        padding: 2px 0; margin: 8px 0;
    }
    .user-bubble {
        display: block;
        width: fit-content;
        max-width: 78%;
        margin-left: auto;
        background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
        color: #ffffff;
        padding: 11px 16px;
        border-radius: 18px 18px 4px 18px;
        font-size: 1.0rem; line-height: 1.55;
        box-shadow: 0 2px 6px rgba(99, 102, 241, .35);
        overflow-wrap: anywhere;
    }
    .user-bubble code {
        background: rgba(255,255,255,.22); color: #fff; padding: 0 4px; border-radius: 4px;
    }
    .user-bubble .ts {
        display: block; text-align: right;
        font-size: .68rem; opacity: .75; margin-top: 5px;
    }

    /* ---- tool chip + timestamp row ---- */
    .meta-row { margin-top: 8px; display: flex; align-items: center; gap: 8px; }
    .chip {
        display: inline-block; background: #eef2ff; color: #4338ca;
        border: 1px solid #e0e7ff; border-radius: 999px;
        padding: 1px 10px; font-size: .74rem; font-weight: 600;
    }
    .ts-plain { font-size: .72rem; color: #98a2b3; }

    /* ---- sidebar ---- */
    section[data-testid="stSidebar"] { border-right: 1px solid #ececf1; }
    .sample-q { font-size: .84rem; color: #475467; margin: 2px 0; }

    /* ---- chat input ---- */
    div[data-testid="stChatInputContainer"] textarea {
        border-radius: 18px !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().strftime("%H:%M")


def _inline(text: str) -> str:
    """Markdown-lite -> safe HTML for the user bubble."""
    out = html.escape(str(text))
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return out.replace("\n", "<br>")


def _welcome_message() -> dict:
    return {"role": "assistant", "content": FIXED_GREETING, "tool": "greeting",
            "ts": _now(), "welcome": True}


def _render_message(m: dict, *, streaming: bool = False, placeholder=None) -> None:
    role = m.get("role", "assistant")

    if role == "user":
        with st.chat_message("user", avatar="🧑‍💼"):
            st.markdown(
                f'<div class="user-bubble">{_inline(m["content"])}'
                f'<span class="ts">🕐 {m.get("ts", _now())}</span></div>',
                unsafe_allow_html=True,
            )
        return

    with st.chat_message("assistant", avatar="🤖"):
        if m.get("welcome"):
            st.markdown('<span class="welcome-badge">Welcome to Ask Your CRM</span>',
                        unsafe_allow_html=True)
        if streaming and placeholder is not None:
            placeholder.markdown(m["content"])
        else:
            st.markdown(m["content"])
        st.markdown(
            f'<div class="meta-row"><span class="chip">🔧 {html.escape(str(m.get("tool", "assistant")))}</span>'
            f'<span class="ts-plain">🕐 {m.get("ts", _now())}</span></div>',
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Sidebar — About + stats + controls (no example buttons anywhere)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### ℹ️ About")
    st.markdown(
        "**Ask Your CRM** turns a traditional CRM database into a conversational "
        "assistant — no screens, filters or reports needed.\n\n"
        "- 💬 Ask **any** question in plain English\n"
        "- 🧠 Agent pipeline: classify intent → write SQL from the **live schema** "
        "→ run it → summarize\n"
        "- ✍️ Performs **actions** too (create follow-ups, mark them Done, update records)\n"
        "- 🔒 Reads are **SELECT-only**, writes are **INSERT/UPDATE/DELETE-only** "
        "(never DROP/ALTER)\n"
        "- 🕐 Runs 24/7 on the free Groq tier"
    )

    st.markdown("**Sample questions you can type**")
    for sample in (
        "Which leads haven't been contacted in the last 7 days?",
        "What are my top 5 opportunities by value?",
        "Which salesperson has the highest conversion rate this month?",
        "Total pipeline value in Negotiation stage.",
        "Create follow-up tasks for all leads not contacted in 7 days.",
    ):
        st.markdown(f'<div class="sample-q">• {html.escape(sample)}</div>',
                    unsafe_allow_html=True)

    st.divider()
    stats = db_stats()
    total = sum(stats.values())
    st.markdown(f"**🗄️ Database:** {total} records across {len(stats)} tables")
    cols = st.columns(2)
    for i, (table, count) in enumerate(stats.items()):
        cols[i % 2].caption(f"{table}: **{count}**")

    st.divider()
    st.caption(f"🤖 Model: `{active_model()}` · Groq free tier")

    c1, c2 = st.columns(2)
    if c1.button("🗑️ Clear chat", use_container_width=True):
        st.session_state.messages = [_welcome_message()]
        st.rerun()

    transcript = "\n\n".join(
        f"**{'You' if m['role'] == 'user' else 'Assistant'}** ({m.get('ts', '')}):\n\n"
        f"{m['content']}"
        for m in st.session_state.get("messages", [])
    )
    c2.download_button(
        "⬇️ Export", data=transcript or "# Ask Your CRM",
        file_name="ask-your-crm-chat.md", mime="text/markdown",
        use_container_width=True,
    )


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = [_welcome_message()]

st.title("🤖 Ask Your CRM")

for _m in st.session_state.messages:
    _render_message(_m)

# ---------------------------------------------------------------------------
# Single chat input box (no pre-made question buttons)
# ---------------------------------------------------------------------------
prompt = st.chat_input("Ask anything about your CRM — leads, pipeline, follow-ups, actions…")

if prompt:
    user_msg = {"role": "user", "content": prompt, "ts": _now()}
    _render_message(user_msg)

    history = [m for m in st.session_state.messages if not m.get("welcome")]
    st.session_state.messages.append(user_msg)

    with st.chat_message("assistant", avatar="🤖"):
        status = st.status("Understanding your request…", expanded=False)
        stream_box = st.empty()
        answer = ""
        result = {"answer": "", "tool": "assistant", "intent": "chat", "data": None}
        started = time.time()

        for event in ask_crm_events(prompt, history):
            if event["event"] == "stage":
                status.update(label=event["label"], expanded=False)
            elif event["event"] == "delta":
                answer += event["text"]
                stream_box.markdown(answer + " ▍")
            elif event["event"] == "done":
                result = event["result"]

        final_answer = result.get("answer") or answer
        stream_box.markdown(final_answer)
        elapsed = time.time() - started
        icon = "❌" if result.get("intent") == "error" else "✅"
        status.update(label=f"{icon} {result.get('tool', 'assistant')} · {elapsed:.1f}s",
                      state="complete" if result.get("intent") != "error" else "error",
                      expanded=False)

        st.markdown(
            f'<div class="meta-row"><span class="chip">🔧 {html.escape(str(result.get("tool", "assistant")))}</span>'
            f'<span class="ts-plain">🕐 {_now()}</span></div>',
            unsafe_allow_html=True,
        )

    st.session_state.messages.append({
        "role": "assistant",
        "content": final_answer,
        "tool": result.get("tool", "assistant"),
        "data": result.get("data"),
        "ts": _now(),
    })
