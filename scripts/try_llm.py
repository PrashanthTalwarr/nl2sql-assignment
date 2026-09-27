"""Ask real questions through the full pipeline and print what happens."""
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, llm, pipeline

logging.basicConfig(level=logging.INFO, format="%(message)s")

EXEC = db.get_user("U001")
RAM = db.get_user("U009")


def show(label, user, question, history=None):
    t0 = time.time()
    r = pipeline.ask(user, question, history)
    print("=" * 80)
    print(f"[{label}] Q: {question}")
    print(f"kind={r['kind']}  rows={len(r['rows'])}  verified={r['verified']}  "
          f"time={time.time()-t0:.1f}s  provider={llm.last_provider}")
    if r["sql"]:
        print(f"SQL:\n{r['sql']}")
    print(f"ANSWER: {r['answer']}")
    return r


print("Current periods:", db.get_periods())
print("Providers in order:", llm._providers())

show("EXEC", EXEC, "What are the top 5 accounts by pack units this quarter?")
show("EXEC", EXEC, "What is our market share for Zenovax in the Docetaxel market?")
show("RAM ", RAM, "What is my total revenue in dollars?")
show("RAM ", RAM, "What are my top 5 accounts by pack units?")

q1 = "What are total pack units by NovaPharma product this year?"
r1 = show("EXEC", EXEC, q1)
history = [{"role": "user", "content": q1}, pipeline.assistant_turn(r1)]
show("EXEC", EXEC, "Now break that down by quarter", history)