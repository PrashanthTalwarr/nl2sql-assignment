"""Ambiguity tests: vague, partial names, two names, multi-match, no-match, pronouns, out-of-domain."""
import logging
import os
import sys
from pathlib import Path

os.environ["ANSWER_CACHE"] = "off"          # always hit the model, never the cache
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, pipeline

logging.basicConfig(level=logging.WARNING)

EXEC = db.get_user("U001")
RAM = db.get_user("U009")

CASES = [
    ("EXEC", EXEC, "How are we doing?",                           "default: products, this vs last quarter"),
    ("EXEC", EXEC, "How is Memorial doing?",                      "lookup -> likely several matches -> clarify w/ options"),
    ("EXEC", EXEC, "Show me the Lakeshore account this quarter",  "lookup -> exact name -> query"),
    ("EXEC", EXEC, "Compare Memorial vs Lakeshore by pack units", "ONE lookup, 2 terms -> clarify or combined query"),
    ("EXEC", EXEC, "compare them",                                "no prior question -> clarify"),
    ("EXEC", EXEC, "What's the weather in Boston?",               "out of domain -> clarify w/ examples"),
    ("RAM ", RAM,  "How is Liberty Health Partners doing?",       "lookup scoped to NY Metro -> not found unless local"),
]

for label, user, question, expect in CASES:
    r = pipeline.ask(user, question)
    print("=" * 90)
    print(f"[{label}] {question}")
    print(f"   expect : {expect}")
    print(f"   kind   : {r['kind']}   rows={len(r['rows'])}")
    if r.get("options"):
        print(f"   options: {r['options']}")
    if r.get("sql"):
        print(f"   sql    : {' '.join(r['sql'].split())[:220]}")
    print(f"   answer : {r['answer']}")