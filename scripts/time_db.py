"""Times real queries per role through the scoped views. No LLM involved."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db

ROLES = [("Exec", db.get_user("U001")), ("Director", db.get_user("U003")), ("RAM", db.get_user("U009"))]

QUERIES = {
    "top accounts this quarter": """
        SELECT COALESCE(o.grandparent_org_name, o.org_name) AS account, SUM(s.pack_units) AS units
        FROM sales s JOIN organizations o ON s.org_id = o.org_id
        WHERE s.data_source = 'distributor' AND s.brand_flag = 1 AND s.period_qtr = '2026-Q3'
        GROUP BY account ORDER BY units DESC LIMIT 10""",
    "market share (all history)": """
        WITH nova AS (SELECT SUM(s.pack_units * p.unit_conversion_factor) AS n FROM sales s
                      JOIN products p ON s.ndc = p.ndc
                      WHERE s.data_source = 'distributor' AND s.brand_flag = 1
                        AND p.market_subcategory = 'Docetaxel'),
             mkt  AS (SELECT SUM(s.pack_units * p.unit_conversion_factor) AS m FROM sales s
                      JOIN products p ON s.ndc = p.ndc
                      WHERE s.data_source = 'market_data' AND p.market_subcategory = 'Docetaxel')
        SELECT ROUND(100.0 * n / NULLIF(m, 0), 1) FROM nova, mkt""",
    "volume by territory": """
        SELECT z.territory_name, SUM(s.pack_units) AS units
        FROM sales s JOIN organizations o ON s.org_id = o.org_id JOIN zip_territory z ON o.zip = z.zip
        WHERE s.data_source = 'distributor' AND s.brand_flag = 1
        GROUP BY z.territory_name ORDER BY units DESC""",
}

for label, user in ROLES:
    print(f"\n{label}")
    for name, sql in QUERIES.items():
        t0 = time.time()
        db.run_query(sql, user)
        print(f"  {name:<30} {(time.time() - t0) * 1000:>7.0f} ms")
    t0 = time.time()
    db.lookup_entities(user, "organization", "Memorial")
    print(f"  {'name lookup (Memorial)':<30} {(time.time() - t0) * 1000:>7.0f} ms")