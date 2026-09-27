import sqlite3

conn = sqlite3.connect("pharma.db")
q = lambda sql: conn.execute(sql).fetchall()

print("Row counts:")
for t in ["organizations", "products", "sales", "zip_territory", "users"]:
    print(f"  {t}: {q(f'SELECT COUNT(*) FROM {t}')[0][0]:,}")

print("\nNULL fix worked? (standalone facilities with NULL grandparent):")
print("  ", q("SELECT COUNT(*) FROM organizations WHERE org_id LIKE 'SA%' AND grandparent_org_name IS NULL")[0][0])

print("\nScope check for the 3 mock users (orgs visible):")
print("  Exec (all):        ", q("SELECT COUNT(*) FROM organizations")[0][0])
print("  Director Northeast:", q("SELECT COUNT(*) FROM organizations WHERE zip IN (SELECT zip FROM zip_territory WHERE region_name='Northeast')")[0][0])
print("  RAM New York Metro:", q("SELECT COUNT(*) FROM organizations WHERE zip IN (SELECT zip FROM zip_territory WHERE territory_name='New York Metro')")[0][0])