"""
Builds pharma.db from the schema DDL + generated CSVs.
Schema is never modified: we only CREATE the given tables, INSERT rows, and ADD indexes.
"""
import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema"
GENERATED = SCHEMA / "generated"
DB_PATH = ROOT / "pharma.db"

CSV_TABLES = ["organizations", "products", "zip_territory", "sales"]


def load_csv(conn, table):
    path = GENERATED / f"{table}.csv"
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)                      # first row = column names
        cols = ",".join(header)
        placeholders = ",".join("?" * len(header))
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"

        batch, total = [], 0
        for row in reader:
            # KEY LINE: "" -> NULL, so COALESCE works for standalone facilities
            batch.append([None if v == "" else v for v in row])
            if len(batch) == 50_000:               # insert in chunks, not one by one
                conn.executemany(sql, batch)
                total += len(batch)
                batch = []
        if batch:
            conn.executemany(sql, batch)
            total += len(batch)
    print(f"  {table}: {total:,} rows")


def load_users(conn):
    # Only take the users block from seed_data.sql; everything else comes from CSVs.
    seed = (SCHEMA / "seed_data.sql").read_text(encoding="utf-8")
    users_block = seed[seed.find("INSERT INTO users"):]
    conn.executescript(users_block)
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    print(f"  users: {n} rows")


INDEXES = [
    "CREATE INDEX idx_org_zip    ON organizations(zip)",
    "CREATE INDEX idx_org_gp     ON organizations(grandparent_org_name)",
    "CREATE INDEX idx_sales_org  ON sales(org_id)",
    "CREATE INDEX idx_sales_src  ON sales(data_source, brand_flag)",
    "CREATE INDEX idx_sales_mo   ON sales(mo_offset)",
    "CREATE INDEX idx_sales_qtr  ON sales(period_qtr)",
    "CREATE INDEX idx_sales_ndc  ON sales(ndc)",
    "CREATE INDEX idx_zip_terr   ON zip_territory(territory_name)",
    "CREATE INDEX idx_zip_region ON zip_territory(region_name)",
]


def main():
    if not GENERATED.exists():
        raise SystemExit("Run: python schema/generate_data.py first")
    if DB_PATH.exists():
        DB_PATH.unlink()                           # always rebuild from scratch

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA synchronous = OFF")       # faster bulk load (safe: we can rebuild)

    print("Creating tables (unchanged DDL)...")
    conn.executescript((SCHEMA / "create_tables.sql").read_text(encoding="utf-8"))

    print("Loading data...")
    for t in CSV_TABLES:
        load_csv(conn, t)
    load_users(conn)

    print("Building indexes...")
    for stmt in INDEXES:
        conn.execute(stmt)

    conn.commit()
    conn.execute("VACUUM")                         # compact into one clean file
    conn.close()
    print(f"Done -> {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()