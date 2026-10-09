# 🤖 Ask Your CRM — AI-powered CRM Agent

Turn a traditional CRM database into a conversational assistant. Type **any** question in
plain English and get an accurate, human-friendly answer — no screens, filters, or reports.

> "Which leads haven't been contacted in the last 7 days?"
> → the agent writes real SQL from the **live schema**, executes it safely, and streams a
> ChatGPT-style answer with bullets and **bold** key numbers. The exact SQL and rows
> are captured with every message (`data` field — used by `demo.py` and Export), but
> the chat UI stays clean: no raw-data panel is shown.

---

## 1. Quick start (2 commands after install)

```bash
pip install -r requirements.txt
python database.py            # creates + seeds crm.db (3 salespeople, 6 leads,
                              # 3 customers, 7 opportunities, 5 activities, 4 follow-ups)

streamlit run app.py          # opens the chat UI at http://localhost:8501
```

Add your free Groq API key first (get one at [console.groq.com/keys](https://console.groq.com/keys)):

```bash
# .env
GROQ_API_KEY=gsk_your_key_here
# optional override (auto-fallbacks if unavailable):
# GROQ_MODEL=llama-3.3-70b-versatile
```

Rebuild the demo data any time:

```bash
python database.py --reseed
```

Run the demonstration checklist from the terminal (great for demos):

```bash
python demo.py                # greeting + 9 read queries + a non-CRM question
python demo.py --actions      # ⚠️ mutates DB: bulk stale-lead follow-ups, mark Done
python demo.py --all          # everything
```

---

## 2. Architecture (Text-to-SQL agent pipeline)

```
User question
     |
     v
[1] INTENT CLASSIFIER   -> chat | read | write | general
     |                     (deterministic rules first — greetings are always fixed;
     |                      LLM only for everything else)
     v
[2] SQL GENERATOR       <- live DB schema (build_schema() reads sqlite_master)
     |
     v
[3] SQL EXECUTOR        -> SELECT-only for reads, INSERT/UPDATE/DELETE-only for
     |                     writes; never DROP/ALTER/CREATE. On error -> regenerate
     |                     with the error hint and retry ONCE.
     v
[4] SUMMARIZER          -> streamed, human-friendly ChatGPT-style answer
                           (direct answer line, bullets, **bold** numbers,
                           optional markdown table, insight, follow-up question)
     |
     v
Streamlit chat UI (answer + tool chip; SQL/rows kept in the message's `data`)
```

**Modules**

| File | Responsibility |
|---|---|
| `database.py` | Schema DDL, realistic seed data, `run_query` (SELECT-only) / `run_write` (DML-only), live introspection helpers |
| `tools.py` | `build_schema()`, `execute_sql()`, `execute_action()`, `find_written_row()`, `create_followups_for_stale_leads(days)` |
| `agent.py` | `classify` → `generate_sql` / `generate_action_sql` → execute+retry → `summarize`, the `ask_crm_events` streaming pipeline, Groq client with model fallback + retries |
| `app.py` | ChatGPT-style Streamlit UI: bubbles, streaming, stage indicator, tool chip, sidebar About/stats/Clear/Export — **one chat input, no example buttons, no raw-data panel** |
| `demo.py` | Terminal runner for the test checklist |

**AI model:** Groq `llama-3.3-70b-versatile` (free tier, fast) with automatic fallback to
`openai/gpt-oss-120b` → `qwen/qwen3.8-27b` → `allam-2-7b` if the primary is unavailable
on your key/region — the agent never dies mid-chat. Transient errors get a short retry
with backoff; the UI shows a friendly 🔑 message if the key is invalid/missing.

**Determinism guarantees**

- Greetings/thanks/goodbye → the **exact same fixed reply** every time (matched by rules
  before any model call, so it works even offline).
- Read/write answers are always computed from the **live database** — no hardcoded
  example answers; no example buttons in the UI.

---

## 3. Data flow of one question

1. `app.py` collects the single chat input and calls `ask_crm_events(query, history)`.
2. The pipeline yields `stage` events (progress label), `delta` events (streamed answer
   tokens), and a final `done` event with `{answer, tool, intent, data}`.
3. `data` always carries the executed `sql` + `rows` (or the write result), stored in
   `st.session_state` with each message — printed by `demo.py` and included in the
   sidebar **Export** download (the chat UI itself does not render it).

---

## 4. Deploy free on Streamlit Community Cloud (24/7)

1. Push this folder to a GitHub repo (must contain `requirements.txt`, and commit an
   `.env.example`; **never commit your real `.env`**).
2. Open [share.streamlit.io](https://share.streamlit.io) → **New app** → pick the repo,
   branch, and `app.py`.
3. In **Advanced settings → Secrets** add:

   ```toml
   GROQ_API_KEY = "gsk_your_key_here"
   ```

   (the app also reads `.env`, so a repo `.env` works too — Secrets is safer)
4. Deploy. The app auto-creates and seeds `crm.db` on first load (ephemeral storage —
   use Streamlit's file storage or SQLite-in-a-file on a persistent service if you need
   the data to survive redeploys).
5. Your app stays live at `https://<your-app>.streamlit.app` — free tier, 24/7.

Alternatives: **Hugging Face Spaces** (Streamlit SDK) or **Render/Railway** free tiers
work with the same two commands: `pip install -r requirements.txt && streamlit run app.py`.

---

## 5. Test checklist (example query → expected output)

| # | Ask this… | Expected |
|---|---|---|
| 1 | `hello` | 👋 Fixed greeting, **identical every time** (tool chip: `greeting`) |
| 2 | `Which leads haven't been contacted in the last 7 days?` | Bullet list of the stale leads (**Vikram Rao, Arjun Verma, Karan Singh**) with last-contacted dates + owners; SQL uses `last_contacted < date('now','-7 days')` |
| 3 | `Show me the opportunities currently stuck in the Proposal stage.` | The 3 Proposal opps (**12,00,000 / 6,40,000 / 4,20,000**) with customer + owner names; `WHERE stage='Proposal'` |
| 4 | `Which salesperson has the highest conversion rate this month?` | Names the rep(s) with this-month Closed Won vs total opps, **% rate bolded**; guarded division with `NULLIF` |
| 5 | `What are my top 5 opportunities by value?` | Ranked list or table, biggest first: **Nova Mobile App 15,00,000**, Orion ERP 12,00,000, Nova CRM 8.5L, Apex Analytics 6.4L, Orion AI 5L; `ORDER BY value DESC LIMIT 5` |
| 6 | `Which customers have pending follow-ups?` | Orion Systems (#1, #4-ish) + Nova Retail + Apex Logistics with due dates and owners; `status='Pending'` |
| 7 | `Create follow-up tasks for all leads not contacted in 7 days.` | ✅ **Created 3 follow-up tasks** (Arjun/Karan/Vikram) due tomorrow, each with follow-up id; rerun → "no duplicates" (idempotent) |
| 8 | `How many leads do I have?` | One-liner + breakdown: **6** total by status/source |
| 9 | `What's my average deal size?` | Average over open opps (and/or all), **₹ figure bolded**, lakh equivalent |
| 10 | `Total pipeline value in Negotiation stage.` | **8,50,000 ≈ 8.5 lakh** for the single Negotiation opp |
| 11 | `Who owns the Nova Retail account?` | **Priya Nair** (owner of customer id 2) |
| 12 | `Mark followup 3 as Done.` | ✅ confirmation with the updated record (`status: Done`, rowcount 1) in the answer; SQL + resulting row in `data` (visible via `demo.py` / Export) |
| 13 | `What is the capital of France?` | Normal ChatGPT-style answer (**Paris**) — general branch, no SQL |
| 14 | Anything else (follow-ups, activities, comparisons, math) | Answered dynamically from the live DB — no fixed list anywhere |

Safety checks:

```python
execute_sql("DROP TABLE leads")            -> rejected (SELECT-only)
execute_sql("SELECT 1; DELETE FROM leads") -> rejected (single statement)
execute_action("ALTER TABLE leads ...")    -> rejected (DML-only)
```

---

## 6. Constraints honored

- ✅ **No time limit / 24/7** — stateless per question, free-tier Groq, runs on Streamlit Cloud
- ✅ **ANY question** — SQL + summarization generated per request, never a fixed list
- ✅ **Fixed greeting** — rule-matched before any model call, byte-identical every time
- ✅ **No hardcoded example buttons** — exactly one chat input; the sidebar shows plain-text sample hints, not clickable buttons
- ✅ **Real LLM** — Groq `llama-3.3-70b-versatile` (auto-fallback), streamed answers
- ✅ **Safe SQL** — SELECT-only reads, DML-only writes, single statement, no DROP/ALTER, retry-once on error
- ✅ **Bonus action** — `create_followups_for_stale_leads(days)` + generic INSERT/UPDATE/DELETE actions with read-back confirmation
