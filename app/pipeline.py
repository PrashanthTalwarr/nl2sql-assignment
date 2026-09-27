"""
One chat turn, end to end, as a STREAM OF EVENTS:
  status(understanding) -> plan
  status(looking_up)    -> [optional] scoped name lookup (up to 3 terms) -> resolve
  status(querying)      -> guard + scoped execute (bounded repair loop, status(fixing))
                           + name BACKSTOP: no data on guessed org name(s) -> lookup -> re-query
  data                  -> result table sent as soon as the query finishes
  status(answering)     -> answer streamed token by token (delta events)
  status(verifying)     -> tidy + number verification (+ one correction if needed)
  done                  -> final, authoritative result

ask() consumes the same stream and returns only the final result.

AGENT TRACE (app/trace.py): every LLM call (model, tokens, cost, latency, error), every tool call
(execute_sql, lookup_names, backstop_lookup, verify_numbers: parameters, result summary,
latency, error), and every agent decision (action + planner reasoning) is recorded as a step.
The single JSON log line per request carries the steps plus rolled-up metrics: total steps,
LLM calls, tool calls, tokens, estimated cost, errors, latency.

SQL ON REQUEST (README: "no SQL, no technical details visible to the user unless they ask"):
  SQL is never shown by default. If the user explicitly asks ("show me the SQL", "what query
  did you run?"), the pipeline returns the EXACT query behind their previous answer, taken from
  the conversation history (never regenerated, never re-executed), plus a plain-English,
  step-by-step explanation. No planner call.

FAILURE LADDER (never give up on the first problem; never dead-end the user):
  1. Malformed model JSON      -> one automatic retry (llm._chat_json)
  2. SQL errors                -> up to MAX_REPAIRS repairs with full failure history
  3. Fuzzy names               -> lookup hop / backstop
  4. Still unanswerable        -> explain_failure(): plain-language reason + answerable alternatives
  5. AI service down           -> clear message + one-tap retry
  6. Bug in our code           -> "something went wrong on my side" + retry; details only in logs
  Raw error text is NEVER shown to users (it can leak internals); every failure ends with
  clickable options.

If no written answer passes number verification, the fallback restates WHAT was computed
(the planner's plain-language summary) and points to the exact table. It never states an
unverified number.

"No data" means no cell holds a value: an aggregate with no matching rows returns [[None]],
which is one row but no data, so the backstop still fires for total-style queries.

Bounds (no path can exceed these):
  - Lookup hops: MAX_LOOKUP_HOPS = 1 per question (planner OR backstop, never both).
  - Terms per hop: MAX_LOOKUP_TERMS = 3.
  - Repairs: MAX_REPAIRS (default 3, hard cap 5), shared across the whole request.

Answer cache: key includes user_id (prevents cross-role leaks) and data_through
(a data refresh invalidates old answers). Disable with ANSWER_CACHE=off.
"""
import copy
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict

from . import db, llm, security, verify
from .trace import Trace

log = logging.getLogger("nl2sql")

MAX_REPAIRS = max(0, min(int(os.getenv("MAX_REPAIRS", "3")), 5))
MAX_LOOKUP_HOPS = 1     # at most ONE name-lookup hop per question (planner OR backstop, never both)
MAX_LOOKUP_TERMS = 3    # names resolved together inside that one hop
CACHE_ENABLED = os.getenv("ANSWER_CACHE", "on").lower() in ("1", "true", "on", "yes")
CACHE_TTL_S = int(os.getenv("ANSWER_CACHE_TTL_S", "3600"))
CACHE_MAX = 256

WAC_MESSAGE = ("Pricing (WAC / dollar revenue) isn't available for your access level. "
               "I can show the same analysis by volume instead (pack units or dosing "
               "equivalents). Want me to do that?")
SECURITY_MESSAGE = ("I can't run that request. It asks for data or operations outside "
                    "what this assistant is allowed to access.")
GIVE_UP_MESSAGE = ("I wasn't able to answer that one, even after trying a few different "
                   "approaches. One of these might get you close to what you need:")
SERVICE_DOWN_MESSAGE = ("The AI service I use to understand questions isn't responding right now, "
                        "so I couldn't answer this. That's usually temporary. Tap your question "
                        "below to try again in a moment.")
INTERNAL_ERROR_MESSAGE = ("Something went wrong on my side while answering this, and it has been "
                          "logged for the team. Nothing is wrong with your question. Tap it below "
                          "to try again, or try one of these:")
SAFE_ANSWER = ("Here are the results for your question. The table below shows the exact "
               "figures from your data.")
UNCLEAR_MESSAGE = ("I couldn't pin down exactly which one you mean. Could you give the full "
                   "account name?")
NO_SQL_YET = ("Ask me a data question first, and I can show you the exact query behind "
              "the answer.")

# Deterministic fallback suggestions, used when the LLM can't generate alternatives
DEFAULT_OPTIONS = {
    "exec": ["What are the top 10 accounts by pack units this quarter?",
             "What is our market share for Zenovax in the Docetaxel market?",
             "Rank all territories by total NovaPharma volume"],
    "director": ["Compare territories in my region by pack units this quarter",
                 "What are my top 5 accounts?",
                 "How does 340B volume compare to non-340B volume?"],
    "ram": ["What are my top 5 accounts by pack units?",
            "Show me market share for Zenovax",
            "What are total pack units by product this quarter?"],
}

ORG_NAME_EQ = re.compile(
    r"\b(?:\w+\.)?(grandparent_org_name|parent_org_name|org_name)\s*=\s*'((?:[^']|'')+)'",
    re.IGNORECASE,
)
GENERIC_WORDS = {
    "the", "health", "system", "systems", "medical", "network", "care", "center", "centre",
    "group", "partners", "alliance", "associates", "clinic", "clinical", "hospital",
    "healthcare", "services", "institute", "oncology", "cancer", "infusion", "community",
    "regional", "general", "university", "specialty", "account", "main",
}

# A user explicitly asking to see the SQL behind an answer
SQL_REQUEST = re.compile(
    r"\bsql\b"
    r"|\b(query|queries)\b.*\b(you|used|ran|run|behind|generated|wrote|written)\b"
    r"|\b(show|see|give|generate|share|display|view|print)\b.*\b(query|queries)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Answer cache (thread-safe LRU with TTL)
# ---------------------------------------------------------------------------
_cache = OrderedDict()
_cache_lock = threading.Lock()


def _cache_key(user, question, history, periods):
    raw = json.dumps([user["user_id"], periods["data_through"],
                      " ".join(question.lower().split()), history], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        stored_at, result = item
        if time.time() - stored_at > CACHE_TTL_S:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return copy.deepcopy(result)


def _cache_put(key, result):
    with _cache_lock:
        _cache[key] = (time.time(), copy.deepcopy(result))
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------
def lookup_terms(lookup):
    """Normalize the planner's lookup request to a de-duplicated list of <= 3 terms.
    Accepts the current format ("terms": [...]) and the older one ("term": "...")."""
    raw = lookup.get("terms")
    if not isinstance(raw, list):
        raw = [lookup.get("term", "")]
    terms = []
    for t in raw:
        t = str(t or "").strip()
        if len(t) >= 2 and t.lower() not in (x.lower() for x in terms):
            terms.append(t)
    return terms[:MAX_LOOKUP_TERMS]


def guessed_org_names(sql):
    """Organization names the SQL filters on with an exact '=' match (de-duplicated)."""
    names = [m.group(2).replace("''", "'") for m in ORG_NAME_EQ.finditer(sql or "")]
    return list(dict.fromkeys(names))


def _search_terms(name):
    """Try the full guessed name first, then its most distinctive word."""
    terms = [name]
    words = [w for w in re.findall(r"[A-Za-z0-9&\-]+", name)
             if len(w) >= 3 and w.lower() not in GENERIC_WORDS]
    if words:
        terms.append(words[0])
    return list(dict.fromkeys(terms))


def backstop_lookup(user, guessed):
    """For each guessed exact name (max 3): search the full name, then its distinctive word.
    Returns {guessed_name: {"term": ..., "matches": [...], "exists": bool}}."""
    found = {}
    for name in guessed[:MAX_LOOKUP_TERMS]:
        entry = {"term": name, "matches": [], "exists": False}
        for term in _search_terms(name):
            matches = db.lookup_entities(user, "organization", term)
            if any(str(m.get("name", "")).lower() == name.lower() for m in matches):
                entry = {"term": term, "matches": matches, "exists": True}
                break
            if matches:
                entry = {"term": term, "matches": matches, "exists": False}
                break
        found[name] = entry
    return found


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _result(answer, kind, sql=None, data=None, verified=None, summary="",
            corrected=False, options=None):
    data = data or {"columns": [], "rows": [], "truncated": False}
    return {"answer": answer, "kind": kind, "sql": sql, "verified": verified,
            "summary": summary, "corrected": corrected, "options": options or [], **data}


def _options(plan):
    raw = plan.get("options") or []
    if not isinstance(raw, list):
        return []
    return [str(o).strip()[:120] for o in raw if str(o).strip()][:4]


def _unique(options):
    """Remove duplicate chips (case-insensitive), keep order, max 4."""
    seen, out = set(), []
    for o in options:
        k = str(o).strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(str(o).strip())
    return out[:4]


def _default_options(user):
    return list(DEFAULT_OPTIONS.get(user.get("role"), DEFAULT_OPTIONS["ram"]))


def assistant_turn(result):
    """How an assistant reply is stored in history: answer, plain-language summary, and the
    SQL that ran. Follow-ups reuse the SQL; 'show me the SQL' returns it with its summary."""
    content = result["answer"]
    if result.get("summary"):
        content += f"\n\n[How I got this]\n{result['summary']}"
    if result.get("sql"):
        content += f"\n\n[SQL used]\n{result['sql']}"
    return {"role": "assistant", "content": content}


def is_sql_request(question):
    return bool(SQL_REQUEST.search(question or ""))


def last_sql_turn(history):
    """(sql, summary) from the most recent assistant turn that ran a query, or (None, None)."""
    for turn in reversed(history or []):
        if turn.get("role") != "assistant":
            continue
        content = turn.get("content") or ""
        m = re.search(r"\[SQL used\]\n(.*)\Z", content, re.S)
        if m:
            s = re.search(r"\[How I got this\]\n(.*?)(?:\n\n\[SQL used\]|\Z)", content, re.S)
            return m.group(1).strip(), (s.group(1).strip() if s else "")
    return None, None


def _normalize(sql):
    return " ".join((sql or "").split()).lower()


def _tidy(text):
    """Deterministic cleanup: drop markdown tables, headings, and bold."""
    lines = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("|"):
            continue
        if s.startswith("#"):
            s = s.lstrip("#").strip()
        if s:
            lines.append(s)
    return "\n".join(lines).replace("**", "").strip()


def _safe_answer(summary):
    """Fallback when no written answer could be verified: restate WHAT was computed (the planner's
    plain-language summary, which carries no data figures of its own) and point to the exact table."""
    s = (summary or "").strip().rstrip(".")
    if s:
        return f"Here's what I found: {s}. The table below shows the exact figures from your data."
    return SAFE_ANSWER


def _has_data(rows):
    """True if any cell holds a value. An aggregate with no matches returns [[None]], which
    is 'no data' even though it is one row."""
    return any(cell is not None for row in rows for cell in row)


def _status(stage, **extra):
    return {"type": "status", "stage": stage, **extra}


def _decision(tr, plan):
    """Record an agent decision (action + planner reasoning) in the trace."""
    tr.event("decision", action=plan.get("action"), reasoning=plan.get("reasoning"),
             summary=plan.get("summary") or None)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def ask_stream(user, question, history=None):
    rid = uuid.uuid4().hex[:8]
    t0 = time.time()
    tr = Trace()
    history = history or []
    messages = history + [{"role": "user", "content": question}]
    periods = db.get_periods()
    failures = []
    hops = {"used": 0}
    flags = {"unverified_first": [], "safe": False, "corrected": False, "explained": False,
             "cache_hit": False, "lookup": None, "plan_cache_read_tokens": 0}
    timings = {}
    key = _cache_key(user, question, history, periods)

    def mark(name, started):
        timings[name] = int((time.time() - started) * 1000)

    def done(result, error=None):
        if (CACHE_ENABLED and result["kind"] in ("query", "refuse")
                and not flags["safe"] and not flags["cache_hit"]):
            _cache_put(key, result)
        log.info(json.dumps({
            "request_id": rid, "user_id": user["user_id"], "role": user["role"],
            "question": question, "answer": (result.get("answer") or "")[:2000],
            "kind": result["kind"], "sql": result.get("sql"),
            "rows": len(result["rows"]), "repair_attempts": len(failures),
            "failure_errors": [f["error"] for f in failures],
            "lookup_hops": hops["used"], "lookup": flags["lookup"],
            "options": result.get("options"), "explained": flags["explained"],
            "verified": result.get("verified"),
            "unverified_numbers": flags["unverified_first"],
            "corrected": flags["corrected"], "safe_answer_used": flags["safe"],
            "cache_hit": flags["cache_hit"],
            "plan_cache_read_tokens": flags["plan_cache_read_tokens"],
            "timings_ms": timings,
            "latency_ms": int((time.time() - t0) * 1000), "error": error,
            "provider": llm.last_provider,
            "metrics": tr.summary(),
            "steps": tr.steps,
        }, default=str))
        return {"type": "done", "result": result}

    def unexpected(e, sql=None):
        """An exception we didn't plan for: classify it. Details go to logs, never to users."""
        if isinstance(e, llm.LLMUnavailable):
            return done(_result(SERVICE_DOWN_MESSAGE, "error", sql, options=[question]),
                        f"LLM unavailable: {e}")
        if isinstance(e, ValueError):                  # model output unusable even after retry
            return done(_result(GIVE_UP_MESSAGE, "error", sql, options=_default_options(user)),
                        f"unusable model output: {e}")
        log.exception("Internal error while answering request %s", rid)
        return done(_result(INTERNAL_ERROR_MESSAGE, "error", sql,
                            options=_unique([question] + _default_options(user))[:3]),
                    f"internal error: {e!r}")

    def explain_and_suggest(reason, sql=None):
        """Last resort: plain-language explanation + answerable alternatives (clickable)."""
        try:
            exp = llm.explain_failure(user, messages, periods, reason, trace=tr)
            message = _tidy(exp.get("message"))
            options = _options(exp)
            if message:
                flags["explained"] = True
                return done(_result(message, "error", sql,
                                    options=options or _default_options(user)), reason)
        except Exception as e:
            reason = f"{reason}; explain failed: {e}"
        return done(_result(GIVE_UP_MESSAGE, "error", sql, options=_default_options(user)), reason)

    # SQL on request: the user explicitly asked to see the query behind the previous answer.
    # Returned deterministically from history (never regenerated, never re-executed),
    # plus a plain-English explanation of what it does.
    if is_sql_request(question):
        last_sql, why = last_sql_turn(history)
        tr.event("decision", action="show_sql", reasoning="explicit request to see the SQL; "
                 + ("returning the previous query from history" if last_sql
                    else "no previous query in this conversation"))
        if not last_sql:
            yield done(_result(NO_SQL_YET, "clarify", options=_default_options(user)))
            return
        yield _status("explaining")
        try:
            steps = _tidy(llm.explain_sql(last_sql[:4000], why, trace=tr))
        except Exception:
            steps = ""
        text = "Here's the exact query behind my previous answer, and what it does:"
        text += f"\n{steps}" if steps else (f"\n{why}" if why else "")
        yield done(_result(text, "sql", last_sql, summary=why))
        return

    # 0. Answer cache
    if CACHE_ENABLED:
        cached = _cache_get(key)
        if cached:
            flags["cache_hit"] = True
            tr.event("cache_hit", kind=cached["kind"])
            if cached["kind"] == "query":
                yield {"type": "data", "sql": cached["sql"], "columns": cached["columns"],
                       "rows": cached["rows"], "truncated": cached["truncated"]}
            yield {"type": "delta", "text": cached["answer"]}
            yield done(cached)
            return

    # 1. Plan
    yield _status("understanding")
    started = time.time()
    try:
        plan = llm.plan(user, messages, periods, trace=tr)
        flags["plan_cache_read_tokens"] = llm.last_cache_read_tokens
        _decision(tr, plan)
    except Exception as e:
        yield unexpected(e)
        return

    # 1b. Planner-requested lookup: ONE hop, up to 3 terms, scoped to the user's data
    if plan.get("action") == "lookup":
        lookup = plan.get("lookup") or {}
        kind = lookup.get("kind", "organization")
        terms = lookup_terms(lookup)
        if not terms:
            yield done(_result(UNCLEAR_MESSAGE, "clarify", options=_default_options(user)))
            return
        yield _status("looking_up", term=", ".join(terms))
        try:
            started_l = time.time()
            matches_by_term = {t: db.lookup_entities(user, kind, t) for t in terms}
            tr.tool("lookup_names", {"kind": kind, "terms": terms}, started_l,
                    result={t: len(m) for t, m in matches_by_term.items()})
            hops["used"] += 1
            flags["lookup"] = {"trigger": "planner", "terms": terms,
                               "matches": {t: len(m) for t, m in matches_by_term.items()}}
            plan = llm.resolve(user, messages, periods, plan,
                               {"kind": kind, "terms": terms}, matches_by_term, trace=tr)
            _decision(tr, plan)
        except Exception as e:
            yield unexpected(e)
            return
        if plan.get("action") == "lookup":             # a 2nd hop is not allowed
            tr.event("hop_cap", reasoning="a second lookup hop was requested; not allowed")
            plan = {"action": "clarify", "message": UNCLEAR_MESSAGE, "options": []}
    mark("plan_ms", started)

    action = plan.get("action")
    if action == "refuse":
        yield done(_result(_tidy(plan.get("message")) or "I can't help with that one.", "refuse"))
        return
    if action == "clarify" or action != "query":
        yield done(_result(_tidy(plan.get("message")) or "Could you rephrase that?",
                           "clarify", options=_options(plan) or _default_options(user)))
        return

    # 2. Execute: bounded repair loop, plus the name backstop (within the hop budget)
    sql = plan.get("sql", "")
    tried = set()
    yield _status("querying")
    started = time.time()
    while True:                                         # outer loop: allows the backstop re-query
        while True:                                     # inner loop: repair on errors
            norm = _normalize(sql)
            if norm in tried:
                yield explain_and_suggest(
                    "Attempts to fix the query kept producing the same failing query. "
                    f"Last errors: {[f['error'] for f in failures][-2:]}", sql)
                return
            tried.add(norm)

            started_q = time.time()
            try:
                try:
                    data = db.run_query(sql, user)
                except Exception as e:
                    tr.tool("execute_sql", {"sql": sql}, started_q,
                            error=f"{type(e).__name__}: {e}"[:300])
                    raise
                tr.tool("execute_sql", {"sql": sql}, started_q,
                        result={"rows": len(data["rows"]), "columns": data["columns"],
                                "truncated": data["truncated"]})
                break
            except security.WacRestricted:
                yield done(_result(WAC_MESSAGE, "refuse"))
                return
            except security.SecurityViolation as e:
                yield done(_result(SECURITY_MESSAGE, "refuse"), str(e))
                return
            except db.QueryTimeout as e:
                error = (f"{e} Simplify: aggregate earlier, filter on indexed columns "
                         "(mo_offset, period_qtr, data_source), and avoid unnecessary joins.")
            except (security.GuardError, sqlite3.Error) as e:
                error = str(e)
            except Exception as e:
                yield unexpected(e, sql)
                return

            if len(failures) >= MAX_REPAIRS:
                yield explain_and_suggest(
                    f"The query failed {len(failures) + 1} times. Most recent error: {error}", sql)
                return

            failures.append({"plan": plan, "error": error})
            yield _status("fixing", attempt=len(failures))
            try:
                plan = llm.repair(user, messages, periods, failures, trace=tr)
                _decision(tr, plan)
            except Exception as e:
                yield unexpected(e, sql)
                return
            if plan.get("action") != "query":
                yield done(_result(_tidy(plan.get("message")) or "Could you rephrase that?",
                                   "clarify", options=_options(plan) or _default_options(user)))
                return
            sql = plan.get("sql", "")

        # --- Name backstop: the planner guessed exact org name(s) and got no data back ---
        guessed = guessed_org_names(sql)
        if _has_data(data["rows"]) or hops["used"] >= MAX_LOOKUP_HOPS or not guessed:
            break

        try:
            started_b = time.time()
            found = backstop_lookup(user, guessed)
            tr.tool("backstop_lookup", {"guessed": guessed}, started_b,
                    result={n: {"exists": e["exists"], "matches": len(e["matches"])}
                            for n, e in found.items()})
        except Exception:
            break                                       # backstop can only help, never hurt
        hops["used"] += 1
        all_exist = all(entry["exists"] for entry in found.values())
        flags["lookup"] = {"trigger": "backstop", "guessed": list(found.keys()),
                           "terms": [e["term"] for e in found.values()],
                           "matches": {e["term"]: len(e["matches"]) for e in found.values()},
                           "all_exist": all_exist}
        if all_exist:
            break                                       # real names, genuinely no data

        missing = [name for name, entry in found.items() if not entry["exists"]]
        terms = [entry["term"] for entry in found.values()]
        yield _status("looking_up", term=", ".join(terms))
        note = (f"Your previous query filtered on the exact organization name(s) {missing} and "
                "returned no rows. Those exact names do not exist in this user's data.")
        matches_by_term = {entry["term"]: entry["matches"] for entry in found.values()}
        try:
            new_plan = llm.resolve(user, messages, periods, plan,
                                   {"kind": "organization", "terms": terms},
                                   matches_by_term, note=note, trace=tr)
            _decision(tr, new_plan)
        except Exception:
            break                                       # can't resolve: answer with the empty result

        if new_plan.get("action") == "query" and _normalize(new_plan.get("sql")) not in tried:
            plan, sql = new_plan, new_plan.get("sql", "")
            yield _status("querying")
            continue
        if new_plan.get("action") in ("clarify", "refuse"):
            yield done(_result(_tidy(new_plan.get("message")) or UNCLEAR_MESSAGE,
                               "clarify", options=_options(new_plan) or _default_options(user)))
            return
        break
    mark("query_ms", started)

    # 3. Send the table immediately, then stream the answer
    summary = plan.get("summary", "")
    yield {"type": "data", "sql": sql, **data}
    yield _status("answering")

    started = time.time()
    try:
        pieces = []
        for piece in llm.answer_stream(question, summary, data, periods, trace=tr):
            pieces.append(piece)
            yield {"type": "delta", "text": piece}
        text = _tidy("".join(pieces))
        mark("answer_ms", started)

        # 4. Verify every number
        yield _status("verifying")
        started = time.time()
        started_v = time.time()
        bad = verify.unverified_numbers(text, data, question, summary, periods)
        tr.tool("verify_numbers", {"answer_chars": len(text)}, started_v,
                result={"unverified": bad})
        flags["unverified_first"].extend(bad)
        if bad:
            flags["corrected"] = True
            text = _tidy(llm.answer(question, summary, data, periods, feedback=bad, trace=tr))
            started_v = time.time()
            bad = verify.unverified_numbers(text, data, question, summary, periods)
            tr.tool("verify_numbers", {"answer_chars": len(text), "attempt": 2}, started_v,
                    result={"unverified": bad})
        if bad:
            text, flags["safe"] = _safe_answer(summary), True
            tr.event("safe_fallback", reasoning="answer still contained unverified numbers")
        mark("verify_ms", started)
    except Exception as e:
        tr.event("answer_error", error=f"{type(e).__name__}: {e}"[:300])
        text, flags["safe"] = _safe_answer(summary), True   # table still shown: the data is intact

    yield done(_result(text, "query", sql, data, verified=True, summary=summary,
                       corrected=flags["corrected"]))


def ask(user, question, history=None):
    """Non-streaming wrapper: same pipeline, returns only the final result."""
    result = None
    for event in ask_stream(user, question, history):
        if event["type"] == "done":
            result = event["result"]
    return result