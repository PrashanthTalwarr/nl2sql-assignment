"""
Database access. Every query:
  guard check -> fresh read-only connection -> scoped views -> execute with timeout.
"""
import os
import re
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from . import security

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "pharma.db")).resolve()

MAX_ROWS = 1000
QUERY_TIMEOUT_S = 15


class QueryTimeout(Exception):
    pass


def _connect():
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_user(user_id):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT user_id, email, full_name, role, territory_name, region_name, "
            "can_view_wac FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def run_query(sql, user, params=()):
    sql = security.validate_sql(sql, user)

    conn = _connect()
    try:
        security.build_scoped_views(conn, user)

        deadline = time.monotonic() + QUERY_TIMEOUT_S
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 100_000)

        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchmany(MAX_ROWS + 1)
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e):
                raise QueryTimeout(f"Query exceeded {QUERY_TIMEOUT_S}s and was stopped.")
            raise

        columns = [d[0] for d in cur.description] if cur.description else []
        return {
            "columns": columns,
            "rows": [list(r) for r in rows[:MAX_ROWS]],
            "truncated": len(rows) > MAX_ROWS,
        }
    finally:
        conn.close()


def lookup_entities(user, kind, term):
    """Find names matching a partial term, WITHIN THE USER'S SCOPE.
    Runs through run_query, so the scoped views apply: a RAM can't use lookups
    to discover which accounts exist in other territories."""
    term = re.sub(r"[%_\\]", "", str(term or "")).strip()[:60]
    if len(term) < 2:
        return []
    like = f"%{term}%"
    if kind == "product":
        sql = ("SELECT DISTINCT drug_name AS name, 'product' AS level, market_subcategory AS detail "
               "FROM products WHERE drug_name LIKE ? OR generic_name LIKE ? ORDER BY name LIMIT 10")
        params = (like, like)
    else:
        sql = ("SELECT name, level, COUNT(*) AS org_count FROM ("
               " SELECT grandparent_org_name AS name, 'health system' AS level"
               "   FROM organizations WHERE grandparent_org_name LIKE ?"
               " UNION ALL SELECT parent_org_name, 'parent group'"
               "   FROM organizations WHERE parent_org_name LIKE ?"
               " UNION ALL SELECT org_name, 'facility'"
               "   FROM organizations WHERE org_name LIKE ? AND org_type = 'Facility'"
               ") GROUP BY name, level ORDER BY org_count DESC LIMIT 10")
        params = (like, like, like)
    result = run_query(sql, user, params)
    return [dict(zip(result["columns"], row)) for row in result["rows"]]


@lru_cache(maxsize=1)
def get_periods():
    """Current period labels from the data refresh (not today's date).
    Trusted internal read, so it queries the base table directly."""
    conn = _connect()
    try:
        month, quarter, data_through = conn.execute(
            "SELECT period_mo, period_qtr, MAX(week_ending_date) FROM sales WHERE wk_offset = 0"
        ).fetchone()
    finally:
        conn.close()
    year, q = quarter.split("-Q")
    y, qn = int(year), int(q)
    prev = f"{y - 1}-Q4" if qn == 1 else f"{y}-Q{qn - 1}"
    return {"month": month, "quarter": quarter, "prev_quarter": prev,
            "year": year, "data_through": data_through}


@lru_cache(maxsize=1)
def get_reference():
    """Regions and their territories. zip_territory is unrestricted reference data
    (per the security model), so every user's planner may know these names."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT region_name, territory_name FROM zip_territory "
            "ORDER BY region_name, territory_name"
        ).fetchall()
    finally:
        conn.close()
    by_region = {}
    for region, territory in rows:
        by_region.setdefault(region, []).append(territory)
    return "\n".join(f"  {r}: {', '.join(t)}" for r, t in by_region.items())