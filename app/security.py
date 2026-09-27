"""
Access control. The LLM is NEVER trusted for security.

1. Scoped views: per request we create TEMP views named `sales` and
   `organizations` that shadow the real tables and contain only the rows
   this user may see. For non-execs, `wac` is not in the view at all.
2. SQL guard: rejects anything that isn't a single read-only SELECT,
   blocks direct access to the real tables (main.*), and blocks `wac`
   for non-execs.

Error types (the pipeline treats them differently):
  SecurityViolation -> unsafe request, NEVER retried
  WacRestricted     -> pricing request from a non-exec, NEVER retried
  GuardError        -> malformed query (empty, not a SELECT, multi-statement), may be retried
"""
import re

# Every column of the real sales table, in order.
SALES_COLUMNS = [
    "sale_id", "org_id", "ndc", "drug_name", "data_source", "brand_flag",
    "pack_units", "total_mg", "wac", "transaction_date", "week_ending_date",
    "state", "specialty", "period_wk", "period_mo", "period_qtr",
    "wk_offset", "mo_offset",
]


class GuardError(ValueError):
    """Query rejected. Base class for all guard rejections."""


class SecurityViolation(GuardError):
    """Query tried to do something unsafe. Never retried."""


class WacRestricted(GuardError):
    """Non-exec tried to touch pricing data. Never retried."""


def _lit(value):
    # Safe SQL string literal. Values come from our own users table, not user input.
    return "'" + str(value).replace("'", "''") + "'"


def can_view_wac(user):
    return user["role"] == "exec" and user["can_view_wac"] == 1


def scope_predicate(user):
    """SQL condition limiting organizations to the user's scope (None = no limit)."""
    role = user["role"]
    if role == "exec":
        return None
    if role == "director":
        # region_name specifically: "South Central" is BOTH a territory and a region
        return (f"zip IN (SELECT zip FROM main.zip_territory "
                f"WHERE region_name = {_lit(user['region_name'])})")
    if role == "ram":
        return (f"zip IN (SELECT zip FROM main.zip_territory "
                f"WHERE territory_name = {_lit(user['territory_name'])})")
    raise ValueError(f"Unknown role: {role}")


def build_scoped_views(conn, user):
    """Create TEMP views that shadow the real tables for this connection only."""
    pred = scope_predicate(user)
    where = f" WHERE {pred}" if pred else ""

    # organizations: only in-scope rows
    conn.execute(
        f"CREATE TEMP VIEW organizations AS SELECT * FROM main.organizations{where}"
    )

    # sales: only in-scope rows, and no wac column unless exec
    cols = SALES_COLUMNS if can_view_wac(user) else [c for c in SALES_COLUMNS if c != "wac"]
    sales_where = (
        f" WHERE org_id IN (SELECT org_id FROM main.organizations{where})" if pred else ""
    )
    conn.execute(
        f"CREATE TEMP VIEW sales AS SELECT {', '.join(cols)} FROM main.sales{sales_where}"
    )


FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|vacuum|reindex)\b",
    re.IGNORECASE,
)
MAIN_REF = re.compile(r"\bmain\s*\.", re.IGNORECASE)   # would bypass the scoped views
USERS_REF = re.compile(r"\busers\b", re.IGNORECASE)     # no reason to read the users table
WAC_REF = re.compile(r"\bwac\b", re.IGNORECASE)


def validate_sql(sql, user):
    """Raise a GuardError subclass if the SQL isn't safe. Returns the cleaned SQL."""
    s = (sql or "").strip().rstrip(";").strip()

    # 1. Security checks FIRST, so an unsafe query is always classified as a
    #    violation (never retried), even if it's also malformed.
    if FORBIDDEN.search(s):
        raise SecurityViolation("Query contains a forbidden keyword.")
    if MAIN_REF.search(s):
        raise SecurityViolation("Direct access to base tables is not allowed.")
    if USERS_REF.search(s):
        raise SecurityViolation("The users table is not queryable.")
    if not can_view_wac(user) and WAC_REF.search(s):
        raise WacRestricted("Pricing (WAC) is not available for your role.")

    # 2. Shape checks (malformed but not malicious; the pipeline may retry these)
    if not s:
        raise GuardError("Empty query.")
    if ";" in s:
        raise GuardError("Only one statement is allowed.")
    if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
        raise GuardError("Only SELECT queries are allowed.")

    return s