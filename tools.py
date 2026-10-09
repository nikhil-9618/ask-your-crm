"""Ask Your CRM — tools used by the agent.

- SCHEMA / build_schema()               : schema text the LLM sees (live DB introspection + notes)
- execute_sql()                         : SELECT-only execution (safe read path)
- execute_action()                      : INSERT/UPDATE/DELETE-only execution (safe write path)
- read_row()                            : fetch one record (used to confirm a write)
- create_followups_for_stale_leads()    : the dedicated bulk-action helper (idempotent)
- db_stats()                            : row counts for the UI sidebar
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Dict, List, Optional

from database import db_stats, run_query, run_write, table_info, table_names

# Fallback schema description (also used if the live DB cannot be read).
SCHEMA = """
TABLES:

salespeople(id, name, email, region)

leads(id, name, company, email, phone, source, status, owner_id,
      created_at, last_contacted, estimated_value)
      status values: New, Contacted, Qualified, Lost
      owner_id -> salespeople.id

customers(id, name, company, email, owner_id, since)
      owner_id -> salespeople.id

opportunities(id, name, customer_id, stage, value, owner_id,
              created_at, expected_close)
      stage values: Prospecting, Proposal, Negotiation, Closed Won, Closed Lost
      customer_id -> customers.id
      owner_id -> salespeople.id

activities(id, type, subject, related_to, related_id, owner_id, date)
      related_to values: lead, opportunity, customer

followups(id, customer_id, description, due_date, status, owner_id)
      status values: Pending, Done
      customer_id -> customers.id (NULL when the follow-up belongs to a lead,
      in which case the lead is named inside description)
"""

# Business notes that do not come from the table definitions themselves.
NOTES = """
NOTES:
- All date columns are TEXT in ISO format 'YYYY-MM-DD'.
- owner_id / customer_id / related_id are foreign keys (no SQL FOREIGN KEY
  constraints are declared, so JOIN manually).
- money columns: leads.estimated_value and opportunities.value (INR rupees).
- followups.customer_id is NULL for follow-ups created from stale leads;
  the lead's name and company are written into followups.description.
- allowed lead status: New, Contacted, Qualified, Lost
- allowed opportunity stage: Prospecting, Proposal, Negotiation, Closed Won, Closed Lost
- allowed followup status: Pending, Done
"""

_FORBIDDEN = (
    "drop",
    "alter",
    "attach",
    "detach",
    "pragma",
    "load_extension",
    "vacuum",
    "create ",
    "replace into",  # only INSERT/UPDATE/DELETE are allowed through execute_action
)

_READ_RE = re.compile(r"^\s*(select|with)\b", re.I)
_WRITE_RE = re.compile(r"^\s*(insert|update|delete)\b", re.I)


def build_schema() -> str:
    """Read the *live* database schema (tables + columns) and append business notes."""
    try:
        lines = []
        for name in table_names():
            cols = table_info(name)
            col_txt = ", ".join(f"{c['name']} {c['type'] or 'TEXT'}" for c in cols)
            lines.append(f"{name}({col_txt})")
        if not lines:
            return SCHEMA.strip()
        return "LIVE SCHEMA (from sqlite_master):\n\n" + "\n".join(lines) + "\n" + NOTES
    except Exception:
        return SCHEMA.strip()


def _normalize(sql: str) -> str:
    """Strip markdown fences and stray commentary the LLM sometimes adds.

    Multiple statements are NOT silently truncated here — they are rejected by
    the validators below so the agent can retry with the error hint.
    """
    s = (sql or "").strip()
    s = re.sub(r"^```(?:sql)?", "", s.strip())
    s = re.sub(r"```$", "", s.strip())
    return s.strip()


def _is_single_statement(sql: str) -> bool:
    return ";" not in sql.strip().rstrip(";").strip()


def _has_forbidden(sql: str) -> Optional[str]:
    low = f" {sql.strip().lower()} "
    for word in _FORBIDDEN:
        if word in low:
            return f"forbidden keyword '{word.strip()}'"
    return None


def execute_sql(sql: str) -> Dict:
    """Safe read path: SELECT/WITH only, single statement, no DDL."""
    sql = _normalize(sql)
    if not sql:
        return {"error": "Empty SQL."}
    if not _READ_RE.match(sql):
        return {"error": "Only SELECT queries are allowed on the read path."}
    if not _is_single_statement(sql):
        return {"error": "Only one statement at a time is allowed."}
    bad = _has_forbidden(sql)
    if bad:
        return {"error": f"Query rejected: {bad}."}
    try:
        rows = run_query(sql)
        return {"rows": rows, "row_count": len(rows), "sql": sql}
    except Exception as e:  # SQLite errors (unknown column, bad syntax, ...)
        return {"error": str(e), "sql": sql}


def execute_action(sql: str) -> Dict:
    """Safe write path: INSERT/UPDATE/DELETE only, single statement, never DDL."""
    sql = _normalize(sql)
    if not sql:
        return {"error": "Empty SQL."}
    if not _WRITE_RE.match(sql):
        return {"error": "Only INSERT, UPDATE or DELETE statements are allowed on the write path."}
    if not _is_single_statement(sql):
        return {"error": "Only one statement at a time is allowed."}
    bad = _has_forbidden(sql)
    if bad:
        return {"error": f"Action rejected: {bad}."}
    try:
        result = run_write(sql)
        return {"success": True, "last_id": result["last_id"],
                "rowcount": result["rowcount"], "sql": sql}
    except Exception as e:
        return {"error": str(e), "sql": sql}


def read_row(table: str, row_id) -> Optional[Dict]:
    """Fetch a single row by id from a known table (used to confirm writes)."""
    if table not in table_names() or not str(row_id).isdigit():
        return None
    try:
        rows = run_query(f"SELECT * FROM {table} WHERE id = ?", (int(row_id),))
        return rows[0] if rows else None
    except Exception:
        return None


def find_written_row(sql: str, last_id=None) -> Optional[Dict]:
    """Best-effort read-back of the record an INSERT/UPDATE just touched."""
    m = re.match(r"\s*(?:insert\s+into|update|delete\s+from)\s+([a-z_]+)", sql, re.I)
    if not m:
        return None
    table = m.group(1).lower()

    if sql.strip().lower().startswith("insert") and last_id:
        return read_row(table, last_id)

    if table not in table_names():
        return None
    where = re.search(r"\bid\s*=\s*(\d+)", sql, re.I)
    if where:
        return read_row(table, where.group(1))
    return None


def create_followups_for_stale_leads(days: int = 7) -> Dict:
    """Dedicated action: create a Pending follow-up for every lead that has not
    been contacted in `days` days (Lost leads are ignored).

    Idempotent: re-running the same request does not create duplicates.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    due = (date.today() + timedelta(days=1)).isoformat()

    stale = run_query(
        """SELECT l.id, l.name, l.company, l.owner_id, l.last_contacted,
                  s.name AS owner_name
           FROM leads l
           LEFT JOIN salespeople s ON s.id = l.owner_id
           WHERE l.last_contacted < ? AND l.status != 'Lost'
           ORDER BY l.last_contacted""",
        (cutoff,),
    )

    created, skipped, items = [], [], []
    for lead in stale:
        description = f"Follow up with stale lead {lead['name']} ({lead['company']})"
        already = run_query(
            "SELECT id FROM followups WHERE description = ? AND status = 'Pending'",
            (description,),
        )
        entry = {
            "lead": lead["name"],
            "company": lead["company"],
            "last_contacted": lead["last_contacted"],
            "owner": lead.get("owner_name") or "Unassigned",
        }
        if already:
            skipped.append(entry)
            continue
        result = run_write(
            """INSERT INTO followups (customer_id, description, due_date, status, owner_id)
               VALUES (NULL, ?, ?, 'Pending', ?)""",
            (description, due, lead.get("owner_id") or 1),
        )
        entry["followup_id"] = result["last_id"]
        entry["due_date"] = due
        created.append(entry)

    return {
        "days": days,
        "cutoff": cutoff,
        "due_date": due,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "items": created,
        "skipped": skipped,
        "sql": f"-- bulk action: create_followups_for_stale_leads(days={days})",
    }
