"""Ask Your CRM — database layer.

Creates the SQLite CRM database, seeds it with realistic demo data and exposes
two small, safe helpers used by the agent:

    run_query(sql, params)  -> SELECT only, returns list[dict]
    run_write(sql, params)  -> INSERT / UPDATE / DELETE only (never DDL)

Run this file directly to (re)build the demo database:
    python database.py              # create + seed if empty
    python database.py --reseed     # wipe and rebuild the demo data
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import date, timedelta
from typing import Dict, List

# Windows pipes default to cp1252 and choke on the ✅ emoji in prints.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Absolute path so the DB lives next to the code no matter where it is launched from.
DB_PATH = os.getenv("CRM_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "crm.db"
)

DDL = """
CREATE TABLE IF NOT EXISTS salespeople (
    id INTEGER PRIMARY KEY,
    name TEXT,
    email TEXT,
    region TEXT
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    name TEXT,
    company TEXT,
    email TEXT,
    phone TEXT,
    source TEXT,
    status TEXT,
    owner_id INTEGER,
    created_at TEXT,
    last_contacted TEXT,
    estimated_value REAL
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT,
    company TEXT,
    email TEXT,
    owner_id INTEGER,
    since TEXT
);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY,
    name TEXT,
    customer_id INTEGER,
    stage TEXT,
    value REAL,
    owner_id INTEGER,
    created_at TEXT,
    expected_close TEXT
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    type TEXT,
    subject TEXT,
    related_to TEXT,
    related_id INTEGER,
    owner_id INTEGER,
    date TEXT
);

CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER,
    description TEXT,
    due_date TEXT,
    status TEXT,
    owner_id INTEGER
);
"""

_TABLES = ("salespeople", "leads", "customers", "opportunities", "activities", "followups")

# Only these statements may ever run through the write helper (no DROP/ALTER/CREATE).
_WRITE_RE = re.compile(r"^\s*(insert|update|delete)\b", re.I)
_READ_RE = re.compile(r"^\s*(select|with)\b", re.I)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _d(days_ago: int = 0) -> str:
    """ISO date string (YYYY-MM-DD) for `days_ago` days in the past."""
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _f(days_ahead: int = 0) -> str:
    """ISO date string (YYYY-MM-DD) for `days_ahead` days in the future."""
    return (date.today() + timedelta(days=days_ahead)).isoformat()


def _single_statement(sql: str) -> bool:
    body = sql.strip().rstrip(";").strip()
    return ";" not in body


def init_db(reseed: bool = False) -> bool:
    """Create tables and seed demo data. Returns True if the DB was (re)seeded."""
    conn = _connect()
    c = conn.cursor()

    if reseed:
        for t in _TABLES:
            c.execute(f"DROP TABLE IF EXISTS {t}")

    c.executescript(DDL)

    if not reseed and c.execute("SELECT COUNT(*) FROM salespeople").fetchone()[0] > 0:
        conn.close()
        return False

    # ---------------- Seed: 3 salespeople ----------------
    salespeople = [
        (1, "Aarav Sharma", "aarav@bharathcloud.com", "South"),
        (2, "Priya Nair", "priya@bharathcloud.com", "West"),
        (3, "Rohan Mehta", "rohan@bharathcloud.com", "North"),
    ]
    c.executemany("INSERT INTO salespeople VALUES (?,?,?,?)", salespeople)

    # ---------------- Seed: 6 leads (mixed source / status / last_contacted) ----------------
    leads = [
        (1, "Vikram Rao", "TechNova", "vikram@technova.in", "9876500011",
         "Website", "New", 1, _d(10), _d(12), 250000),
        (2, "Sneha Kapoor", "BrightSoft", "sneha@brightsoft.in", "9876500012",
         "Referral", "Contacted", 2, _d(3), _d(2), 480000),
        (3, "Arjun Verma", "CloudPeak", "arjun@cloudpeak.in", "9876500013",
         "LinkedIn", "New", 3, _d(9), _d(15), 320000),
        (4, "Meera Iyer", "DataHive", "meera@datahive.in", "9876500014",
         "Event", "Qualified", 1, _d(20), _d(1), 750000),
        (5, "Karan Singh", "FinEdge", "karan@finedge.in", "9876500015",
         "Website", "New", 2, _d(14), _d(14), 180000),
        (6, "Anita Desai", "MediCore", "anita@medicore.in", "9876500016",
         "Referral", "Contacted", 3, _d(5), _d(4), 560000),
    ]
    c.executemany("INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?)", leads)

    # ---------------- Seed: 3 customers ----------------
    customers = [
        (1, "Ravi Menon", "Orion Systems", "ravi@orion.in", 1, _d(400)),
        (2, "Divya Pillai", "Nova Retail", "divya@nova.in", 2, _d(220)),
        (3, "Sameer Khan", "Apex Logistics", "sameer@apex.in", 3, _d(90)),
    ]
    c.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", customers)

    # ---------------- Seed: 7 opportunities (values up to 15 lakh) ----------------
    opportunities = [
        (1, "Orion ERP Upgrade", 1, "Proposal", 1200000, 1, _d(6), _f(15)),
        (2, "Nova CRM Rollout", 2, "Negotiation", 850000, 2, _d(10), _f(10)),
        (3, "Apex Analytics", 3, "Proposal", 640000, 3, _d(4), _f(25)),
        (4, "Orion Support Renewal", 1, "Closed Won", 300000, 1, _d(8), _d(5)),
        (5, "Nova Mobile App", 2, "Prospecting", 1500000, 2, _d(20), _f(45)),
        (6, "Apex Dashboard", 3, "Proposal", 420000, 3, _d(2), _f(30)),
        (7, "Orion AI Add-on", 1, "Closed Lost", 500000, 1, _d(45), _d(20)),
    ]
    c.executemany("INSERT INTO opportunities VALUES (?,?,?,?,?,?,?,?)", opportunities)

    # ---------------- Seed: 5 activities ----------------
    activities = [
        (1, "call", "Intro call", "lead", 1, 1, _d(12)),
        (2, "email", "Proposal sent", "opportunity", 1, 1, _d(3)),
        (3, "meeting", "Demo", "lead", 4, 1, _d(1)),
        (4, "call", "Follow-up", "lead", 2, 2, _d(2)),
        (5, "email", "Pricing details", "opportunity", 3, 3, _d(4)),
    ]
    c.executemany("INSERT INTO activities VALUES (?,?,?,?,?,?,?)", activities)

    # ---------------- Seed: 4 follow-ups (Pending + Done mix) ----------------
    followups = [
        (1, 1, "Send revised proposal", _f(2), "Pending", 1),
        (2, 2, "Schedule contract call", _f(5), "Pending", 2),
        (3, 3, "Quarterly check-in", _d(1), "Pending", 3),
        (4, 1, "Send invoice", _d(3), "Done", 1),
    ]
    c.executemany("INSERT INTO followups VALUES (?,?,?,?,?,?)", followups)

    conn.commit()
    conn.close()
    print("✅ CRM database seeded at", DB_PATH)
    return True


def run_query(sql: str, params=()) -> List[Dict]:
    """Run a read-only statement (SELECT / WITH) and return rows as dicts."""
    if not _READ_RE.match(sql.strip()) or not _single_statement(sql):
        raise ValueError("run_query only allows a single SELECT/WITH statement.")
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def run_write(sql: str, params=()) -> Dict:
    """Run a single INSERT/UPDATE/DELETE. Never allows DROP/ALTER/CREATE."""
    if not _WRITE_RE.match(sql.strip()) or not _single_statement(sql):
        raise ValueError("run_write only allows a single INSERT/UPDATE/DELETE statement.")
    conn = _connect()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return {"last_id": cur.lastrowid, "rowcount": cur.rowcount}
    finally:
        conn.close()


def table_info(table: str) -> List[Dict]:
    """Live column list for one table (used to build the schema shown to the LLM)."""
    conn = _connect()
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def table_names() -> List[str]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return [r["name"] for r in rows]
    finally:
        conn.close()


def db_stats() -> Dict[str, int]:
    """Row counts per table — used by the UI sidebar."""
    stats = {}
    for t in _TABLES:
        try:
            stats[t] = run_query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
        except Exception:
            stats[t] = 0
    return stats


if __name__ == "__main__":
    import sys

    # Windows consoles default to cp1252 and choke on the ✅ emoji.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    init_db(reseed="--reseed" in sys.argv)
