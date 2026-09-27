"""
Number verification (grounding check).

Every number the answer states must be traceable to the query result, the question,
the query description, or the data period labels. This is checked deterministically
IN CODE. The LLM is never asked to grade its own answer.

Limitation (by design): this verifies that each number EXISTS in the data, not that a
sentence USES it correctly. Semantic correctness would need an LLM-as-judge pass
validated against human labels.
"""
import re

# Numbers the ANSWER states. Lookbehind skips fragments like the "3" in "Q3".
NUMBER_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
# Every digit group inside a label ("2026-09-19" -> 2026, 9, 19) for the ALLOWED set.
DIGITS_RE = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(text):
    vals = []
    for m in NUMBER_RE.finditer(str(text)):
        try:
            vals.append(float(m.group().replace(",", "")))
        except ValueError:
            pass
    return vals


def _parts(text):
    return [float(x) for x in DIGITS_RE.findall(str(text))]


def _allowed(result, question, summary, periods):
    allowed = set()
    for row in result["rows"]:
        for cell in row:
            if isinstance(cell, (int, float)) and not isinstance(cell, bool):
                allowed.add(float(cell))
            elif cell is not None:
                allowed.update(_parts(cell))            # "2026-Q3" -> 2026, 3
    for col in result["columns"]:
        allowed.update(_parts(col))
    allowed.update(_parts(question))
    allowed.update(_parts(summary))
    for value in (periods or {}).values():               # "2026-09-19" -> 2026, 9, 19
        allowed.update(_parts(value))
    allowed.add(float(len(result["rows"])))
    return allowed


def _matches(value, allowed):
    if abs(value) <= 10 and value == int(value):
        return True                                     # counts / ordinals: "top 5", "3 quarters"
    for a in allowed:
        if abs(value - a) <= 0.5:                       # rounding: 2563.0 vs 2563
            return True
        if a and abs(value - a) / abs(a) <= 0.001:
            return True
        if 0 < abs(a) <= 1 and abs(value - a * 100) <= 0.5:
            return True                                 # 0.241 stated as 24.1%
    return False


def unverified_numbers(answer, result, question, summary, periods=None):
    """Numbers in the answer that can't be traced to the data. Empty list = verified."""
    allowed = _allowed(result, question, summary, periods)
    return [v for v in numbers_in(answer) if not _matches(v, allowed)]