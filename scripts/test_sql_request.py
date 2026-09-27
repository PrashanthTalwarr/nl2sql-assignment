"""SQL-on-request tests. The explainer is stubbed, so no LLM calls."""
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


# Detection
for q in ["show me the SQL", "what query did you run?", "generate the sql that was used",
          "Can I see the query behind that?"]:
    check(f"Detects: {q!r}", pipeline.is_sql_request(q))
for q in ["What are my top 5 accounts?", "How is Memorial doing?", "Show me Zenovax market share"]:
    check(f"Ignores normal question: {q!r}", not pipeline.is_sql_request(q))

# Extraction from history
prev = {"answer": "Liberty leads with 2,563 units.", "summary": "Top accounts this quarter.",
        "sql": "SELECT 1 AS x"}
history = [{"role": "user", "content": "Top accounts this quarter?"}, pipeline.assistant_turn(prev)]
sql, why = pipeline.last_sql_turn(history)
check("Extracts the exact SQL", sql == "SELECT 1 AS x", sql)
check("Extracts the summary", why == "Top accounts this quarter.", why)

# End to end, with the explainer stubbed out (no LLM)
real_explain = llm.explain_sql
llm.explain_sql = lambda sql, summary: "- Counts paid sales\n- Ranks accounts"
try:
    r = pipeline.ask(EXEC, "show me the SQL", history)
    check("Returns kind 'sql' with the exact query", r["kind"] == "sql" and r["sql"] == "SELECT 1 AS x")
    check("Includes step-by-step reasoning", "- Counts paid sales" in r["answer"])
    r = pipeline.ask(EXEC, "show me the SQL", [])
    check("No previous question -> helpful clarify", r["kind"] == "clarify" and r["options"])
finally:
    llm.explain_sql = real_explain

print(f"\n{sum(results)}/{len(results)} checks passed")