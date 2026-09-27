"""Security proof. Runs real queries as each role and checks what they can see."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, security

EXEC = db.get_user("U001")
DIR = db.get_user("U003")
RAM = db.get_user("U009")

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")


def blocked(sql, user, err=security.GuardError):
    try:
        db.run_query(sql, user)
        return False
    except err:
        return True


TERR_SQL = ("SELECT DISTINCT z.territory_name FROM organizations o "
            "JOIN zip_territory z ON o.zip = z.zip ORDER BY 1")
for label, user in [("Exec", EXEC), ("Director", DIR), ("RAM", RAM)]:
    t0 = time.time()
    terrs = [r[0] for r in db.run_query(TERR_SQL, user)["rows"]]
    print(f"  {label} sees {len(terrs)} territories ({time.time()-t0:.2f}s)")

ram_terrs = [r[0] for r in db.run_query(TERR_SQL, RAM)["rows"]]
dir_terrs = [r[0] for r in db.run_query(TERR_SQL, DIR)["rows"]]
check("RAM sees only New York Metro", ram_terrs == ["New York Metro"], str(ram_terrs))
check("Director sees only Northeast territories",
      set(dir_terrs) == {"New York Metro", "New England"}, str(dir_terrs))

counts = {}
for label, user in [("Exec", EXEC), ("Director", DIR), ("RAM", RAM)]:
    t0 = time.time()
    counts[label] = db.run_query("SELECT COUNT(*) FROM sales", user)["rows"][0][0]
    print(f"  {label} sales rows: {counts[label]:,} ({time.time()-t0:.2f}s)")
check("Sales scoping Exec > Director > RAM > 0",
      counts["Exec"] > counts["Director"] > counts["RAM"] > 0)

texas = db.run_query(
    "SELECT COUNT(*) FROM organizations o JOIN zip_territory z ON o.zip = z.zip "
    "WHERE z.territory_name = 'Texas'", RAM)["rows"][0][0]
check("RAM asking for Texas gets nothing", texas == 0, f"rows={texas}")

exec_cols = db.run_query("SELECT * FROM sales LIMIT 1", EXEC)["columns"]
ram_cols = db.run_query("SELECT * FROM sales LIMIT 1", RAM)["columns"]
check("Exec view includes wac", "wac" in exec_cols)
check("RAM view has NO wac column", "wac" not in ram_cols)
check("Guard blocks SUM(wac) for RAM", blocked("SELECT SUM(wac) FROM sales", RAM, security.WacRestricted))
check("Guard blocks SUM(wac) for Director", blocked("SELECT SUM(wac) FROM sales", DIR, security.WacRestricted))
check("Exec can SUM(wac)", db.run_query("SELECT SUM(wac) FROM sales", EXEC)["rows"][0][0] > 0)

check("Blocks main.sales bypass", blocked("SELECT COUNT(*) FROM main.sales", RAM))
check("Blocks DROP TABLE", blocked("DROP TABLE sales", EXEC))
check("Blocks multi-statement", blocked("SELECT 1; DELETE FROM sales", EXEC))
check("Blocks reading users table", blocked("SELECT * FROM users", EXEC))

print(f"\n{sum(results)}/{len(results)} checks passed")