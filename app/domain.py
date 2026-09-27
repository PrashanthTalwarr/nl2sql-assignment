"""
Loads the business docs into one string for the planner prompt.

Why not RAG: the 8 docs are small and nearly every query needs the same core
rules (what 'sales' means, market share formula, offsets, hierarchy).
There's nothing to retrieve selectively, so retrieval would only add a
failure mode (missed chunk -> wrong SQL) without improving accuracy.
"""
from functools import lru_cache
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

DOC_ORDER = [
    "data_source_guide.md",
    "metric_definitions.md",
    "market_classification.md",
    "org_hierarchy.md",
    "period_offsets.md",
    "security_model.md",
    "account_analytics.md",
    "product_analytics.md",
]


@lru_cache(maxsize=1)
def domain_knowledge():
    parts = []
    for name in DOC_ORDER:
        path = DOCS_DIR / name
        if path.exists():
            parts.append(f"===== {name} =====\n{path.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(parts)