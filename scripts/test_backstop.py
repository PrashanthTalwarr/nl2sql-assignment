"""Lookup + backstop tests.
Tests 1-2 (no LLM): name detection and term normalization.
Tests 3-5 use real LLM calls for resolve / answer, with a FAKE planner to force each case."""
import json
import logging
import os
import sys
from pathlib import Path

os.environ["ANSWER_CACHE"] = "off"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, llm, pipeline

EXEC = db.get_user("U001")
results = []


def check(name, ok, detail=""):
    ok = bool(ok)
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")


class Capture(logging.Handler):
    """Grab the pipeline's JSON log line so we can inspect lookup fields."""
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        try:
            self.records.append(json.loads(record.getMessage()))
        except ValueError:
            pass


cap = Capture()
logging.getLogger("nl2sql").addHandler(cap)
logging.getLogger("nl2sql").setLevel(logging.INFO)


def fake_plan(result):
    return lambda user, messages, periods, **kw: result


def query_plan(sql):
    return {"action": "query", "sql": sql,
            "summary": "Paid NovaPharma pack units for the named account(s), all available data."}


BASE = ("SELECT COALESCE(o.grandparent_org_name, o.org_name) AS account, "
        "SUM(s.pack_units) AS pack_units FROM sales s JOIN organizations o ON s.org_id = o.org_id "
        "WHERE s.data_source = 'distributor' AND s.brand_flag = 1 ")

# --- Test 1: detection (no LLM) ---
check("Detects guessed health-system name",
      pipeline.guessed_org_names(BASE + "AND o.grandparent_org_name = 'Memorial'") == ["Memorial"])
check("Detects two guessed names",
      pipeline.guessed_org_names("WHERE org_name = 'Memorial' OR org_name = 'Lakeshore'")
      == ["Memorial", "Lakeshore"])
check("Detects escaped quote in name",
      pipeline.guessed_org_names("WHERE org_name = 'St. Mary''s Clinic'") == ["St. Mary's Clinic"])
check("Ignores LIKE filters", pipeline.guessed_org_names("WHERE org_name LIKE '%Memorial%'") == [])
check("Distinctive word skips generic words",
      pipeline._search_terms("Memorial Hospital") == ["Memorial Hospital", "Memorial"])

# --- Test 2: term normalization (no LLM) ---
check("Accepts old single 'term' format", pipeline.lookup_terms({"term": "Memorial"}) == ["Memorial"])
check("De-duplicates and caps at 3",
      pipeline.lookup_terms({"terms": ["A1", "a1", "B2", "C3", "D4"]}) == ["A1", "B2", "C3"])
check("Drops empty / 1-char terms", pipeline.lookup_terms({"terms": ["", "x", "Memorial"]}) == ["Memorial"])

real_llm_plan = llm.plan
try:
    # --- Test 3: WRONG guessed name -> backstop fires ---
    llm.plan = fake_plan(query_plan(BASE + "AND o.grandparent_org_name = 'Memorial' GROUP BY account"))
    r = pipeline.ask(EXEC, "How is Memorial doing?")
    rec = cap.records[-1]
    lk = rec.get("lookup") or {}
    check("Wrong name triggers the backstop", lk.get("trigger") == "backstop",
          f"terms={lk.get('terms')} matches={lk.get('matches')}")
    check("Backstop gives a useful result",
          (r["kind"] == "clarify" and r["options"]) or (r["kind"] == "query" and r["rows"]),
          f"kind={r['kind']} options={r['options']}")
    check("Backstop used exactly one hop", rec.get("lookup_hops") == 1)
    print("   ANSWER:", r["answer"])

    # --- Test 4: REAL name, genuinely no data -> backstop must NOT 'fix' it ---
    real_name = db.run_query(
        "SELECT grandparent_org_name FROM organizations "
        "WHERE grandparent_org_name IS NOT NULL LIMIT 1", EXEC)["rows"][0][0]
    escaped = real_name.replace("'", "''")
    llm.plan = fake_plan(query_plan(
        "SELECT SUM(s.pack_units) AS pack_units FROM sales s "
        "JOIN organizations o ON s.org_id = o.org_id "
        f"WHERE o.grandparent_org_name = '{escaped}' AND s.period_qtr = '1999-Q1'"))
    r = pipeline.ask(EXEC, f"How did {real_name} do in 1999?")
    lk = cap.records[-1].get("lookup") or {}
    check("Real name with no data is left alone", lk.get("all_exist") is True and r["kind"] == "query",
          f"name={real_name} kind={r['kind']}")

    # --- Test 5: TWO names in one planner lookup -> both searched, one hop ---
    llm.plan = fake_plan({"action": "lookup",
                          "lookup": {"kind": "organization", "terms": ["Memorial", "Lakeshore"]}})
    r = pipeline.ask(EXEC, "Compare Memorial vs Lakeshore")
    rec = cap.records[-1]
    lk = rec.get("lookup") or {}
    check("Two terms searched in one lookup", lk.get("terms") == ["Memorial", "Lakeshore"],
          f"matches={lk.get('matches')}")
    check("Two-name lookup used exactly one hop", rec.get("lookup_hops") == 1)
    check("Two-name lookup gives a useful result",
          (r["kind"] == "clarify" and r["options"]) or (r["kind"] == "query" and r["rows"]),
          f"kind={r['kind']} options={r['options']}")
    print("   ANSWER:", r["answer"])
finally:
    llm.plan = real_llm_plan          # always restore the real planner

print(f"\n{sum(results)}/{len(results)} checks passed")
