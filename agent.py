"""Ask Your CRM — the agent pipeline.

    user question
         |
         v
    [1] INTENT CLASSIFIER   -> chat | read | write | general      (deterministic rules first, LLM second)
         |
         v
    [2] SQL GENERATOR       <- live DB schema (build_schema())
         |
         v
    [3] SQL EXECUTOR        -> if error -> retry once with the error hint
         |
         v
    [4] SUMMARIZER          -> ChatGPT-style answer (streamed), bullets + bold numbers
         |
         v
    Streamlit chat UI + "Raw data" expander (SQL + rows)

Public API:
    ask_crm_events(query, history) -> generator of event dicts (drives the UI)
    ask_crm(query, history)        -> blocking wrapper returning the final dict
    FIXED_GREETING                -> the deterministic greeting (same every time)
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, Iterator, List, Optional

from dotenv import load_dotenv
from groq import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    Groq,
    NotFoundError,
    RateLimitError,
)

from tools import (
    build_schema,
    create_followups_for_stale_leads,
    execute_action,
    execute_sql,
    find_written_row,
)

load_dotenv()

# Primary model per spec; falls back automatically if the account/region
# does not expose it (keeps the agent alive 24/7 on the free tier).
DEFAULT_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
MODEL_FALLBACKS = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "allam-2-7b",
]
MAX_TOKENS = 4096
MODEL = DEFAULT_MODEL  # shown in the UI (updated to the model actually used)

FIXED_GREETING = (
    "👋 **Hello! I'm your CRM AI Assistant.**\n\n"
    "Ask me anything about your leads, opportunities, customers, salespeople, "
    "follow-ups, or pipeline — in plain English.\n\n"
    "I can also **take actions** like creating follow-up tasks."
)


class LLMError(Exception):
    """Raised when the AI service cannot be reached / no model is usable."""


# ---------------------------------------------------------------------------
# Groq client with model fallback + transient retries
# ---------------------------------------------------------------------------
_client: Optional[Groq] = None
_active_model: Optional[str] = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        key = os.getenv("GROQ_API_KEY")
        if not key or not key.strip():
            raise LLMError(
                "🔑 **GROQ_API_KEY is missing.** Add it to the `.env` file "
                "(`GROQ_API_KEY=gsk_...`) and restart the app."
            )
        _client = Groq(api_key=key.strip())
    return _client


def _candidate_models() -> List[str]:
    primary = os.getenv("GROQ_MODEL", DEFAULT_MODEL)
    ordered = [primary] + [m for m in MODEL_FALLBACKS if m != primary]
    if _active_model in ordered:
        ordered.remove(_active_model)
        ordered.insert(0, _active_model)
    return ordered


def active_model() -> str:
    """Name of the model that will actually be used (for the UI)."""
    return _active_model or os.getenv("GROQ_MODEL", DEFAULT_MODEL)


def _request(messages: List[Dict], *, stream: bool = False, temperature: float = 0.0,
             max_tokens: int = MAX_TOKENS, json_mode: bool = False):
    client = _get_client()
    global _active_model
    last_error: Optional[Exception] = None

    candidates = _candidate_models()
    for index, model in enumerate(candidates):
        attempts = 2 if index == 0 else 1
        for attempt in range(attempts):
            kwargs = dict(model=model, messages=messages,
                          temperature=temperature, max_tokens=max_tokens)
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            if stream:
                kwargs["stream"] = True
            try:
                resp = client.chat.completions.create(**kwargs)
                _active_model = model
                return resp
            except NotFoundError as e:          # model not available on this key -> next model
                last_error = e
                break
            except RateLimitError as e:         # free-tier rate limit -> short backoff
                last_error = e
                time.sleep(1.5 * (attempt + 1))
            except (APIConnectionError, APITimeoutError) as e:
                last_error = e
                time.sleep(1.0)
            except APIStatusError as e:
                status = getattr(e, "status_code", None)
                if status and 500 <= status < 600:
                    last_error = e
                    time.sleep(1.0)
                elif status in (401, 403):
                    raise LLMError(
                        "🔑 **Groq rejected the API key (401).**\n\n"
                        "Open `.env`, paste a valid key after `GROQ_API_KEY=` "
                        "(get one free at console.groq.com/keys) and restart the app."
                    )
                else:
                    raise LLMError(f"Groq API error ({status}): {str(e)[:200]}")
    raise LLMError(
        "⚠️ The AI service is currently unreachable "
        f"({type(last_error).__name__ if last_error else 'no model'}: "
        f"{str(last_error)[:160] if last_error else 'none available'}). Please try again in a moment."
    )


def _text(messages: List[Dict], temperature: float = 0.0, json_mode: bool = False) -> str:
    resp = _request(messages, temperature=temperature, json_mode=json_mode)
    content = ""
    try:
        content = resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        content = ""
    return content.strip()


def _stream_text(messages: List[Dict], temperature: float = 0.3) -> Iterator[str]:
    resp = _request(messages, stream=True, temperature=temperature)
    for chunk in resp:
        try:
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = choices[0].delta
            piece = getattr(delta, "content", None) if delta is not None else None
        except (AttributeError, IndexError):
            continue
        if piece:
            yield piece


def _json_call(prompt: str) -> Dict:
    """Ask for JSON; tolerate models that ignore response_format."""
    messages = [{"role": "user", "content": prompt}]
    try:
        data = _parse_json(_text(messages, temperature=0.0, json_mode=True))
        if data is not None:
            return data
    except LLMError:
        pass  # some models/deployments reject response_format -> try plain text
    data = _parse_json(_text(messages, temperature=0.0))
    return data if data is not None else {}


def _parse_json(raw: str) -> Optional[Dict]:
    if not raw:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", cleaned, flags=re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# STEP 1 — intent classification (deterministic first, LLM second)
# ---------------------------------------------------------------------------
_GREETING_START = {"hi", "hii", "hello", "hey", "yo", "hola", "namaste",
                   "howdy", "greetings", "hiya", "hallo"}
_BYE_WORDS = {"bye", "goodbye", "goodnight", "good", "see", "cya", "later"}
_THANKS_START = {"thanks", "thank", "thx", "ty", "cheers", "ta", "appreciated"}
_SMALLTALK = {
    "how are you", "how r u", "how do you do", "who are you", "who r u",
    "what can you do", "what do you do", "what are you", "are you alive",
    "help", "commands", "options",
}


def _is_greeting(q: str) -> bool:
    """Deterministic small-talk check -> always returns the FIXED greeting."""
    text = re.sub(r"[^a-z0-9'\s]", " ", q.lower())
    words = text.split()
    if not words:
        return False
    joined = " ".join(words)
    if joined in _SMALLTALK:
        return True
    if len(words) <= 4:
        if words[0] in _GREETING_START or words[0] in _THANKS_START:
            return True
        if words[0] in _BYE_WORDS and len(words) <= 3:
            return True
        if words[0] in {"good", "have"} and any(
            w in words for w in ("morning", "afternoon", "evening", "night", "day")
        ):
            return True
        if words[:2] in (["thank", "you"], ["see", "you"], ["ok", "thanks"]):
            return True
    return False


def _heuristic_intent(q: str) -> str:
    low = q.lower()
    if _is_greeting(q):
        return "chat"
    if re.search(
        r"\b(create|add|insert|update|mark|set|change|delete|remove|assign|"
        r"schedule|reschedule|record|log|reassign|complete)\b", low
    ):
        return "write"
    if re.search(
        r"\b(show|list|display|how many|how much|which|who|what|top|total|"
        r"average|avg|mean|sum|count|compare|report|rate|trend|breakdown|"
        r"pipeline|leads?|customers?|opportunit|sales|accounts?|follow-?ups?|"
        r"activities|deals?|conversion|pending|stuck|stale|overdue)\b", low
    ):
        return "read"
    return "general"


def classify(user_query: str) -> str:
    """Return one of: chat | read | write | general."""
    if _is_greeting(user_query):
        return "chat"
    prompt = f"""You are the intent router inside "Ask Your CRM", a CRM chat assistant.
Classify the user's latest message into exactly ONE intent:

- "read"   : asks a question about CRM data or wants a report/list
              (leads, customers, opportunities, salespeople, activities,
               follow-ups, pipeline, conversion, totals, rankings).
              Triggers: show me, list, how many, which, who, what, top,
              total, average, compare, breakdown, pending, stuck, stale.
- "write"  : asks to change CRM data — create / add / update / mark / assign /
              schedule / delete something.
- "chat"   : greeting, thanks, goodbye, or small talk about the assistant itself.
- "general": anything else (general knowledge, math, code, writing, opinions).

Return ONLY JSON, no other text: {{"intent": "read" | "write" | "chat" | "general"}}

User message: "{user_query}"
JSON:"""
    try:
        data = _json_call(prompt)
    except LLMError:
        return _heuristic_intent(user_query)
    intent = str(data.get("intent") or data.get("type") or "").strip().lower()
    if intent in ("read", "write", "chat", "general"):
        return intent
    return _heuristic_intent(user_query)


# ---------------------------------------------------------------------------
# STEP 2 — SQL generation
# ---------------------------------------------------------------------------
def _history_text(history: Optional[List[Dict]]) -> str:
    if not history:
        return "(none yet)"
    lines = []
    for m in history[-6:]:
        role = m.get("role", "?")
        text = str(m.get("content", "")).replace("\n", " ")[:300]
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines) or "(none yet)"


def generate_sql(user_query: str, history: Optional[List[Dict]] = None,
                 previous_error: Optional[str] = None) -> str:
    fix_note = (f"\nYour PREVIOUS SQL FAILED with this error:\n{previous_error}\n"
                "Fix the query and output corrected SQL only.\n") if previous_error else ""
    prompt = f"""You are a SQLite expert writing SQL for the "Ask Your CRM" assistant.
Write ONE SQLite SELECT statement that answers the user's question.

DATABASE SCHEMA (read live from the CRM database):
{build_schema()}

RULES:
- Output ONLY raw SQL. No markdown, no backticks, no comments, no explanation.
- Exactly one statement: SELECT or WITH only. Never INSERT/UPDATE/DELETE/DDL.
- JOIN tables so the result shows readable names (salesperson name, company,
  customer name) instead of raw ids.
- Dates are TEXT 'YYYY-MM-DD':
    last N days  -> date('now', '-N days')
    this month   -> strftime('%Y-%m', col) = strftime('%Y-%m', 'now')
    this week / year -> same idea with strftime.
- "haven't been contacted in 7 days" -> last_contacted < date('now','-7 days')
- "stuck in stage X" -> stage = 'X' AND stage NOT LIKE 'Closed%'
- Conversion rate -> percentage of opportunities with stage 'Closed Won'
  (COUNT over group; divide carefully, guard against /0 with NULLIF).
- Ranking -> ORDER BY ... DESC; add LIMIT for lists (max 100 rows).
- Money columns are INR rupees; do not convert them in SQL.

CONVERSATION SO FAR (for follow-up questions like "and the top 3?"):
{_history_text(history)}

USER QUESTION: {user_query}
{fix_note}
SQL:"""
    return _normalize_sql(_text([{"role": "user", "content": prompt}], temperature=0.0))


def generate_action_sql(user_query: str, history: Optional[List[Dict]] = None,
                        previous_error: Optional[str] = None) -> str:
    fix_note = (f"\nYour PREVIOUS SQL FAILED with this error:\n{previous_error}\n"
                "Fix it and output corrected SQL only.\n") if previous_error else ""
    prompt = f"""You are a SQLite expert writing SQL for the "Ask Your CRM" assistant.
Write exactly ONE INSERT, UPDATE or DELETE statement that performs the user's
request against the CRM database.

DATABASE SCHEMA (read live from the CRM database):
{build_schema()}

RULES:
- Output ONLY raw SQL. No markdown, no backticks, no comments.
- Exactly one statement: INSERT, UPDATE or DELETE only.
- NEVER DROP, ALTER, CREATE, ATTACH or PRAGMA.
- Always include a WHERE clause for UPDATE/DELETE (protect every other row).
- Dates are TEXT 'YYYY-MM-DD':
    due tomorrow -> date('now', '+1 day')
    created now  -> date('now')
- Allowed status values:
    leads.status       = New | Contacted | Qualified | Lost
    opportunities.stage= Prospecting | Proposal | Negotiation | Closed Won | Closed Lost
    followups.status   = Pending | Done

CONVERSATION SO FAR:
{_history_text(history)}

USER REQUEST: {user_query}
{fix_note}
SQL:"""
    return _normalize_sql(_text([{"role": "user", "content": prompt}], temperature=0.0))


def _normalize_sql(sql: str) -> str:
    """Strip markdown fences / trailing semicolon only — never rewrite the SQL.
    Multiple statements are left for the executor to reject (-> retry with hint)."""
    s = (sql or "").strip()
    s = re.sub(r"^```(?:sql)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    return s.strip().rstrip(";").strip()


# ---------------------------------------------------------------------------
# STEP 4 — summarizer (ChatGPT-style, streamed)
# ---------------------------------------------------------------------------
def _summarize_messages(user_query: str, rows: List[Dict], sql: str) -> List[Dict]:
    payload = json.dumps(rows, default=str, ensure_ascii=False)
    truncated = len(payload) > 6000
    payload = payload[:6000] + ("…(truncated)" if truncated else "")
    prompt = f"""You are "Ask Your CRM", a friendly CRM analyst answering inside a
ChatGPT-style chat window. Answer the user's question using ONLY the query
results below.

USER QUESTION:
{user_query}

SQL EXECUTED:
{sql}

ROWS RETURNED: {len(rows)}
DATA (JSON): {payload}

Write the reply exactly like a great ChatGPT answer:
1. Start with ONE direct line that answers the question — no filler
   ("Here is...", "Sure!").
2. Give the details as short bullet points; **bold** important numbers,
   names and dates.
3. If there are more than 3 rows and comparing them helps, use a compact
   markdown table (max 10 rows) and then a couple of bullets; otherwise
   bullets alone are fine.
4. Add one short insight line — what this means for the sales team.
5. Money: amounts are **Indian Rupees (INR)** — always use **₹** (never $),
   thousands separators, plus a lakh equivalent when >= 100,000
   (e.g. ₹1,200,000 ≈ 12.0 lakh).
6. If 0 rows: say plainly that nothing matches, explain the likely reasons
   (date window, status filter, wrong spelling) and suggest one alternative.
7. Finish with ONE short related question the user could ask next.
8. Never invent data that is not in DATA. Do not mention SQL, tables or
   prompts unless the user asks how you did it.
9. Match the user's language. Keep it under 200 words unless the user asked
   for depth."""
    return [{"role": "user", "content": prompt}]


def _action_messages(user_query: str, sql: str, rowcount: int, row: Optional[Dict]) -> List[Dict]:
    prompt = f"""The user asked the CRM assistant to change data. Confirm what happened.

USER REQUEST:
{user_query}

SQL EXECUTED:
{sql}

ROWS AFFECTED: {rowcount}
RESULTING RECORD (JSON): {json.dumps(row, default=str, ensure_ascii=False) if row else "n/a"}

Write a short ChatGPT-style confirmation (2-4 lines max):
- Start with ✅ "**Done.**" (or ⚠️ if something looks wrong).
- Name the record and **bold** the fields that matter (id, status, date...).
- Mention how many rows were affected. No SQL unless it helps."""
    return [{"role": "user", "content": prompt}]


def _general_messages(user_query: str, history: Optional[List[Dict]]) -> List[Dict]:
    prompt = f"""You are "Ask Your CRM", a ChatGPT-style assistant embedded in a CRM for a
sales team. The user asked something that is not about their CRM data.
Answer helpfully, clearly and concisely — exactly the way ChatGPT would.
Be warm, direct and practical. If the topic could connect to their sales
work, add one short final line offering to pull the relevant CRM numbers.

CONVERSATION SO FAR:
{_history_text(history)}

USER QUESTION: {user_query}

ANSWER:"""
    return [{"role": "user", "content": prompt}]


# ---------------------------------------------------------------------------
# Stale-lead bulk action (dedicated helper, deterministic detection)
# ---------------------------------------------------------------------------
_STALE_RE = re.compile(
    r"follow[- ]?ups?|followup|stale|inactive|cold|not (been )?contact|"
    r"haven'?t been contact|hasn'?t been contact|no contact|old leads?|"
    r"re-?engage", re.I)


def _wants_stale_followups(q: str) -> bool:
    low = q.lower()
    if not ("lead" in low or "stale" in low or "inactive" in low):
        return False
    return bool(_STALE_RE.search(low))


def _stale_answer(result: Dict) -> str:
    days = result["days"]
    created, skipped = result["items"], result["skipped"]
    if not created and not skipped:
        return (f"✅ **No action needed** — every lead was contacted within the "
                f"last **{days} days**, so there are no stale leads to follow up.")
    lines = []
    if created:
        lines.append(
            f"✅ Created **{len(created)}** follow-up task"
            f"{'s' if len(created) != 1 else ''} for leads not contacted in the "
            f"last **{days} days** (due **{result['due_date']}**):\n")
        for item in created:
            lines.append(
                f"- **{item['lead']}** ({item['company']}) — last contacted "
                f"**{item['last_contacted']}**, owner **{item['owner']}**, "
                f"follow-up **#{item['followup_id']}**")
    if skipped:
        lines.append(
            f"\n↩️ **{len(skipped)}** already had a pending follow-up "
            f"({'instead: ' if created else ''}" +
            ", ".join(f"**{s['lead']}**" for s in skipped) +
            ") — no duplicates created.")
    lines.append("\n💡 Ask me *“which customers have pending follow-ups?”* to see the full list.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline events
# ---------------------------------------------------------------------------
def _done(result: Dict) -> Dict:
    return {"event": "done", "result": result}


def ask_crm_events(user_query: str, history: Optional[List[Dict]] = None) -> Iterator[Dict]:
    """Yield events for the UI:
       {"event":"stage","label":...}         progress shown above the answer
       {"event":"delta","text":...}          streamed answer chunks
       {"event":"done","result":{answer, tool, intent, data}}
    """
    q = (user_query or "").strip()

    if not q:
        yield _done({"answer": "Type a question above — e.g. *how many leads do I have?*",
                     "tool": "chat", "intent": "chat", "data": None})
        return

    # [1] deterministic greeting branch — always the same fixed reply
    if _is_greeting(q):
        yield {"event": "stage", "label": "Saying hello 👋"}
        yield _done({"answer": FIXED_GREETING, "tool": "greeting",
                     "intent": "chat", "data": None})
        return

    try:
        # ------------------------------------------------ [1] classify
        yield {"event": "stage", "label": "Understanding your request…"}
        intent = classify(q)
        if intent == "chat":
            yield _done({"answer": FIXED_GREETING, "tool": "greeting",
                         "intent": "chat", "data": None})
            return

        # ------------------------------------------------ general chat (ChatGPT-style)
        if intent == "general":
            yield {"event": "stage", "label": "Thinking…"}
            answer = ""
            for piece in _stream_text(_general_messages(q, history), temperature=0.4):
                answer += piece
                yield {"event": "delta", "text": piece}
            yield _done({"answer": answer.strip() or FIXED_GREETING,
                         "tool": "assistant", "intent": "general", "data": None})
            return

        # ------------------------------------------------ [2+] write path
        if intent == "write":
            if _wants_stale_followups(q):
                yield {"event": "stage", "label": "Scanning stale leads…"}
                m = re.search(r"(\d+)\s*day", q.lower())
                days = int(m.group(1)) if m else 7
                result = create_followups_for_stale_leads(days)
                yield _done({
                    "answer": _stale_answer(result),
                    "tool": "create_followups_for_stale_leads",
                    "intent": "write",
                    "data": result,
                })
                return

            yield {"event": "stage", "label": "Planning the write operation…"}
            sql = generate_action_sql(q, history)
            yield {"event": "stage", "label": "Applying the change…"}
            res = execute_action(sql)
            if "error" in res:
                yield {"event": "stage", "label": "Query failed — fixing SQL and retrying…"}
                sql = generate_action_sql(q, history, previous_error=res["error"])
                res = execute_action(sql)
            if "error" in res:
                yield _done({
                    "answer": (f"⚠️ I couldn't complete that action.\n\n"
                               f"**Reason:** {res['error']}\n\n"
                               "Try rephrasing with the record id, e.g. "
                               "*“mark followup 3 as Done”*."),
                    "tool": "action", "intent": "write",
                    "data": {"sql": sql, "error": res["error"]},
                })
                return

            row = find_written_row(sql, res.get("last_id"))
            answer = ""
            for piece in _stream_text(
                _action_messages(q, sql, res.get("rowcount", 0), row), temperature=0.3
            ):
                answer += piece
                yield {"event": "delta", "text": piece}
            if not answer.strip():
                answer = (f"✅ **Done.** Applied `{sql}` — "
                          f"**{res.get('rowcount', 0)}** row(s) affected.")
            yield _done({
                "answer": answer.strip(),
                "tool": "action",
                "intent": "write",
                "data": {"sql": sql, "rowcount": res.get("rowcount", 0),
                         "last_id": res.get("last_id"), "resulting_row": row},
            })
            return

        # ------------------------------------------------ [2] read path: SQL generation
        yield {"event": "stage", "label": "Writing SQL from the live schema…"}
        sql = generate_sql(q, history)
        yield {"event": "stage", "label": "Running the query…"}
        res = execute_sql(sql)
        retried = False

        # ------------------------------------------------ [3] execute + one retry
        if "error" in res:
            retried = True
            yield {"event": "stage", "label": "Query failed — fixing SQL and retrying…"}
            sql = generate_sql(q, history, previous_error=res["error"])
            yield {"event": "stage", "label": "Running the query…"}
            res = execute_sql(sql)

        if "error" in res:
            # A non-CRM question misrouted as "read" still deserves a good answer.
            if _heuristic_intent(q) == "general":
                yield {"event": "stage", "label": "Thinking…"}
                answer = ""
                for piece in _stream_text(_general_messages(q, history), temperature=0.4):
                    answer += piece
                    yield {"event": "delta", "text": piece}
                yield _done({"answer": answer.strip(),
                             "tool": "assistant", "intent": "general", "data": None})
                return
            yield _done({
                "answer": (f"⚠️ I couldn't answer that one.\n\n"
                           f"**What went wrong:** {res['error']}\n\n"
                           "Try rephrasing, or ask me in a different way "
                           "(e.g. name the table/field you care about)."),
                "tool": "sql", "intent": "read",
                "data": {"sql": sql, "error": res["error"]},
            })
            return

        # ------------------------------------------------ [4] summarize (streamed)
        rows = res.get("rows", [])
        yield {"event": "stage", "label": "Composing your answer…"}
        answer = ""
        for piece in _stream_text(_summarize_messages(q, rows, sql), temperature=0.3):
            answer += piece
            yield {"event": "delta", "text": piece}

        yield _done({
            "answer": answer.strip() or "_(no summary produced)_",
            "tool": "text-to-sql (retry used)" if retried else "text-to-sql",
            "intent": "read",
            "data": {"sql": res.get("sql", sql), "rows": rows,
                     "row_count": len(rows)},
        })

    except LLMError as e:
        yield _done({"answer": str(e), "tool": "error", "intent": "error", "data": None})
    except Exception as e:  # never crash the chat UI
        yield _done({
            "answer": f"⚠️ Something went wrong while processing that ({type(e).__name__}: {e}). "
                      "Please try again.",
            "tool": "error", "intent": "error", "data": None,
        })


def ask_crm(user_query: str, history: Optional[List[Dict]] = None) -> Dict:
    """Blocking wrapper around ask_crm_events() — handy for scripts and tests."""
    answer_parts: List[str] = []
    result: Dict = {"answer": "", "tool": "chat", "intent": "chat", "data": None}
    for event in ask_crm_events(user_query, history):
        if event["event"] == "delta":
            answer_parts.append(event["text"])
        elif event["event"] == "done":
            result = event["result"]
    if answer_parts and not result.get("answer"):
        result["answer"] = "".join(answer_parts)
    return result


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    question = " ".join(sys.argv[1:]) or "How many leads do I have?"
    out = ask_crm(question)
    print(f"Q: {question}\n\ntool={out['tool']} intent={out['intent']}\n\n{out['answer']}")
