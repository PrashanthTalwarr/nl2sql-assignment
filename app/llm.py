"""
LLM layer.
  plan()            : question (+ conversation) -> JSON: query / lookup / clarify / refuse
  resolve()         : after a name lookup, continue with the matched names (one hop only,
                      up to 3 search terms resolved together)
  repair()          : retry with the FULL history of failed attempts
  explain_failure() : last resort: plain-language explanation + alternative questions
  answer()          : REAL result rows -> plain-English answer (non-streaming; used for corrections)
  answer_stream()   : same, streamed token by token
  explain_sql()     : plain-English, step-by-step explanation of a query the user explicitly asked to see

Reliability:
  - Every JSON call gets ONE automatic retry if the model returns malformed JSON.
  - LLMUnavailable is raised when every configured provider fails (outage / auth / rate
    limit after retries), so the pipeline can tell "AI service down" apart from other errors.

Temperature 0 where supported: same question -> same SQL (reproducible, testable).

Latency: prompt caching. The planner system prompt is split into a STATIC prefix
(rules + reference data + business docs, identical on every call) and a DYNAMIC suffix
(role, scope, periods). The static prefix is marked cacheable for Anthropic; OpenAI caches
repeated prefixes automatically.

Fallbacks:
  1. SDK retries: transient errors retried 3x with backoff.
  2. Provider failover (for streaming, only BEFORE the first token).
"""
import json
import logging
import os
import re

from dotenv import load_dotenv

from . import db, security
from .domain import domain_knowledge

load_dotenv()

PRIMARY = os.getenv("LLM_PROVIDER", "anthropic").lower()
FALLBACK = os.getenv("LLM_FALLBACK_PROVIDER", "").lower()

MODELS = {
    "openai": os.getenv("OPENAI_MODEL", "gpt-4o"),
    "anthropic": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
}
ANSWER_MODELS = {
    "openai": os.getenv("ANSWER_OPENAI_MODEL") or MODELS["openai"],
    "anthropic": os.getenv("ANSWER_ANTHROPIC_MODEL") or MODELS["anthropic"],
}
KEY_VARS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}

log = logging.getLogger("nl2sql.llm")
_clients = {}
last_provider = None
last_cache_read_tokens = 0


class LLMUnavailable(Exception):
    """Every configured LLM provider failed (outage, auth, or rate limit after retries)."""


def _client(provider):
    if provider not in _clients:
        if provider == "anthropic":
            import anthropic
            _clients[provider] = anthropic.Anthropic(max_retries=3, timeout=60)
        elif provider == "openai":
            from openai import OpenAI
            _clients[provider] = OpenAI(max_retries=3, timeout=60)
        else:
            raise ValueError(f"Unknown LLM provider: {provider}")
    return _clients[provider]


def _anthropic_system(system):
    if isinstance(system, str):
        return system
    static, dynamic = system
    return [{"type": "text", "text": static, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": dynamic}]


def _flat_system(system):
    return system if isinstance(system, str) else "\n\n".join(system)


def _call(provider, system, messages, max_tokens, models):
    global last_cache_read_tokens
    client = _client(provider)
    if provider == "anthropic":
        kwargs = dict(model=models[provider], max_tokens=max_tokens,
                      system=_anthropic_system(system), messages=messages)
        try:
            resp = client.messages.create(temperature=0, **kwargs)
        except TypeError:
            resp = client.messages.create(**kwargs)
        last_cache_read_tokens = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        return "".join(b.text for b in resp.content if b.type == "text")

    kwargs = dict(model=models[provider], max_tokens=max_tokens,
                  messages=[{"role": "system", "content": _flat_system(system)}] + messages)
    try:
        resp = client.chat.completions.create(temperature=0, **kwargs)
    except TypeError:
        resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content


def _stream_call(provider, system, messages, max_tokens, models):
    client = _client(provider)
    if provider == "anthropic":
        kwargs = dict(model=models[provider], max_tokens=max_tokens,
                      system=_anthropic_system(system), messages=messages)
        try:
            manager = client.messages.stream(temperature=0, **kwargs)
        except TypeError:
            manager = client.messages.stream(**kwargs)
        with manager as stream:
            for text in stream.text_stream:
                yield text
        return

    kwargs = dict(model=models[provider], max_tokens=max_tokens, stream=True,
                  messages=[{"role": "system", "content": _flat_system(system)}] + messages)
    try:
        stream = client.chat.completions.create(temperature=0, **kwargs)
    except TypeError:
        stream = client.chat.completions.create(**kwargs)
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


def _providers():
    order = [PRIMARY]
    if FALLBACK and FALLBACK != PRIMARY and os.getenv(KEY_VARS.get(FALLBACK, "")):
        order.append(FALLBACK)
    return order


def _chat(system, messages, max_tokens=2000, models=None):
    global last_provider
    models = models or MODELS
    last_error = None
    for provider in _providers():
        try:
            text = _call(provider, system, messages, max_tokens, models)
            if provider != PRIMARY:
                log.warning("Primary provider failed; served by fallback '%s'", provider)
            last_provider = provider
            return text
        except Exception as e:
            last_error = e
            log.warning("LLM provider '%s' failed: %s", provider, e)
    raise LLMUnavailable(str(last_error)) from last_error


def _stream(system, messages, max_tokens=600, models=None):
    global last_provider
    models = models or MODELS
    last_error = None
    for provider in _providers():
        started = False
        try:
            for piece in _stream_call(provider, system, messages, max_tokens, models):
                if not started:
                    started = True
                    last_provider = provider
                    if provider != PRIMARY:
                        log.warning("Primary provider failed; streaming from fallback '%s'",
                                    provider)
                yield piece
            return
        except Exception as e:
            if started:
                raise
            last_error = e
            log.warning("LLM provider '%s' failed: %s", provider, e)
    raise LLMUnavailable(str(last_error)) from last_error


def _parse_json(text):
    text = re.sub(r"```(?:json)?", "", text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Model did not return JSON: {text[:200]}")
    return json.loads(text[start:end + 1])


def _chat_json(system, messages):
    """JSON call with ONE automatic retry if the model's output isn't valid JSON."""
    text = _chat(system, messages)
    try:
        return _parse_json(text)
    except ValueError:                       # json.JSONDecodeError is a ValueError too
        log.warning("Malformed JSON from model; retrying once")
        retry = list(messages) + [
            {"role": "assistant", "content": text or "(empty response)"},
            {"role": "user", "content": "That response was not valid JSON. Reply again with "
                                        "ONLY the JSON object in the required format."},
        ]
        return _parse_json(_chat(system, retry))


# ---------------------------------------------------------------------------
# Role-aware schema description (the DYNAMIC part of the planner prompt)
# ---------------------------------------------------------------------------

def _schema(user, periods):
    if security.can_view_wac(user):
        wac_line = ("  wac REAL -- dollar revenue (WAC). Actual for distributor, "
                    "estimate for market_data, 0 for hub_dispense\n")
        scope = "Exec: full access to every territory and to pricing (wac)."
    elif user["role"] == "director":
        wac_line = ""
        scope = (f"Director of the {user['region_name']} region. The sales and organizations "
                 "tables ALREADY contain only this region's data. This user has NO pricing "
                 "access; the wac column does not exist for them.")
    else:
        wac_line = ""
        scope = (f"RAM for the {user['territory_name']} territory. The sales and organizations "
                 "tables ALREADY contain only this territory's data. This user has NO pricing "
                 "access; the wac column does not exist for them.")

    return f"""DATABASE (SQLite). Tables you may query: sales, organizations, products, zip_territory.

sales  -- one row per transaction, recorded at FACILITY level
  sale_id, org_id -> organizations.org_id, ndc -> products.ndc, drug_name (UPPERCASE),
  data_source ('distributor' | 'hub_dispense' | 'market_data'),
  brand_flag (1 = NovaPharma, 0 = competitor), pack_units REAL, total_mg REAL,
{wac_line}  transaction_date, week_ending_date ('YYYY-MM-DD'), state, specialty,
  period_wk ('YYYY-WNN'), period_mo ('YYYY-MM'), period_qtr ('YYYY-QN'),
  wk_offset (0 = current week), mo_offset (0 = current month)

organizations
  org_id, org_name, org_type ('Facility'|'Parent'|'Grandparent'), org_status ('Active'|'Inactive'),
  org_archetype ('Hospital'|'Clinic'|'IDN'|'Government'|'Specialty Pharmacy'),
  specialty, city, state, zip -> zip_territory.zip,
  parent_org_id, parent_org_name, grandparent_org_id, grandparent_org_name (NULL for standalone facilities),
  gpo_name ('Onmark'|'ION'|'Unity'|'VitalSource'|NULL), is_340b (1/0)

products
  ndc, drug_name, generic_name, strength, form, brand_flag,
  specialty, market_category, market_subcategory, unit_conversion_factor, mg_equivalent

zip_territory
  zip, state, territory_number, territory_name, region_number, region_name

CURRENT PERIODS (from the data refresh, not today's date):
  current month = {periods['month']}, current quarter = {periods['quarter']},
  previous quarter = {periods['prev_quarter']}, current year = {periods['year']}
  Data runs through {periods['data_through']}: the current month and current quarter are PARTIAL.

ACCESS SCOPE: {scope}
"""


PLAN_RULES = """You are an expert analytics engineer for NovaPharma. Translate the user's LATEST question into ONE SQLite query.
The database schema, current periods, and the user's access scope are given at the END of this prompt.

Respond with ONLY a JSON object (no prose, no code fences):
{"reasoning": "<2-4 sentences: which tables, filters, business rules and defaults apply>",
 "action": "query" | "lookup" | "clarify" | "refuse",
 "sql": "<query only: single SELECT or WITH statement>",
 "lookup": {"kind": "organization" | "product", "terms": ["<each uncertain name fragment the user typed, max 3>"]},
 "summary": "<one sentence in plain business language for a non-technical sales user: what was counted, the time period, and any assumption. No SQL, table names, or column names.>",
 "message": "<clarify/refuse only: what to tell the user>",
 "options": ["<clarify only: 2-4 short example questions the user can click>"]}

BUSINESS RULES (from the domain docs; always apply):
1. "sales", "demand", "volume", "units", "revenue", "how much did we sell" = paid demand:
   data_source = 'distributor' AND brand_flag = 1. Never include hub_dispense unless the user
   explicitly asks about free drug / PAP / hub / "including free drug". Never use market_data
   for NovaPharma's own sales.
2. Default volume measure = SUM(pack_units). Use equivalents = SUM(s.pack_units * p.unit_conversion_factor)
   (JOIN products p ON s.ndc = p.ndc) when the user says "equivalents", compares strengths, or for market share.
3. Market share = NovaPharma equivalents from distributor (brand_flag = 1) / total equivalents from
   market_data, both limited to the same market_subcategory (or market_category if asked at category level).
   Compute numerator and denominator in SEPARATE CTEs, then divide. Never mix sources inside one SUM.
   Report ROUND(100.0 * num / NULLIF(den, 0), 1) AS market_share_pct. For share by territory / region /
   account, group BOTH CTEs by the same key and join on it.
4. "Accounts" / "top accounts" / "health systems" = grandparent level:
   COALESCE(o.grandparent_org_name, o.org_name) AS account, via JOIN organizations o ON s.org_id = o.org_id.
   Use facility or parent level only if the user asks for it.
5. Territory / region: JOIN organizations o ON s.org_id = o.org_id JOIN zip_territory z ON o.zip = z.zip,
   then use z.territory_name / z.region_name. Never use sales.state as a proxy for territory.
6. Time: always use offset / label columns, never date('now') or CURRENT_DATE.
   this month: mo_offset = 0 | last month: mo_offset = 1 | last 3 months / R3M: mo_offset IN (0,1,2)
   prior 3 months / R6M: mo_offset IN (3,4,5) | last 6 months: mo_offset BETWEEN 0 AND 5
   last 12 months: mo_offset BETWEEN 0 AND 11 | this week: wk_offset = 0 | last 4 weeks: wk_offset <= 3
   this quarter: period_qtr = current quarter label | last quarter (alone): mo_offset IN (1,2,3) as the docs define
   quarter comparisons and trends: use period_qtr labels (e.g. current vs previous quarter label)
   this year: substr(period_mo, 1, 4) = current year | trends: GROUP BY period_mo or period_qtr, ORDER BY it.
   If no time period is given for a total or ranking, use all available data and say so in the summary.
   If the result includes the current (partial) month or quarter, say so in the summary.
7. NovaPharma brands (stored UPPERCASE): ZENOVAX, CARBOTREL, GEMTARA, PAXELIUM, ONCOSETRON, CYCLONOVA, LUPREX DEPOT.
8. Rankings need ORDER BY + LIMIT (default LIMIT 10 if no number is given). Detail lists: at most LIMIT 100.
   Round: percentages ROUND(x, 1); units and dollars ROUND(x, 0).
9. Growth %: ROUND(100.0 * (curr - prev) / NULLIF(prev, 0), 1).
10. Give every output column a readable alias (account, pack_units, market_share_pct, ...).
11. Do ALL arithmetic in SQL. If a total, share of total, difference, or growth rate would help answer the
    question, compute it as a column (e.g. SUM(x) OVER () AS total_pack_units, or
    ROUND(100.0 * x / SUM(x) OVER (), 1) AS pct_of_total). The answer step may only restate returned values.
12. Totals: when the user asks for a total ("total sales", "our sales", "how much did we sell") with no
    breakdown requested, return ONE summary row. Do not add a product or time breakdown, or a period
    comparison, that was not asked for.
13. "Sales" by access level: for users WITH pricing access, a sales total includes both
    ROUND(SUM(s.pack_units), 0) AS pack_units and ROUND(SUM(s.wac), 0) AS revenue_wac. For users
    WITHOUT pricing access, "sales" means pack units only: answer it in units (never refuse a plain
    "sales" question).

HANDLING AMBIGUOUS QUESTIONS (apply in this order):
A. Resolve with standard defaults and STATE THEM in the summary; do not ask:
   - no metric ("how is X doing", "performance") -> paid-demand pack units
   - vague overall questions ("how are we doing?") -> pack units by NovaPharma product, current quarter
     vs previous quarter (note the current quarter is partial)
   - "accounts" -> health-system level; "our products" -> the 7 NovaPharma brands
   - informal product spellings ("zenovax", "luprex") -> the exact UPPERCASE brand name
   - territory / region names -> the exact names listed under REFERENCE DATA
B. Partial or informal ORGANIZATION names ("Memorial", "the Lakeshore account", "Liberty"): you do not know
   the exact stored names, so return action "lookup" with kind "organization" and put EVERY uncertain name
   in "terms" together in ONE lookup (e.g. "Compare Memorial vs Lakeshore" -> terms ["Memorial", "Lakeshore"]).
   Maximum 3 terms. NEVER guess an exact organization name in SQL.
C. Pronouns and follow-ups ("that", "those", "same for last year") refer to the previous question and
   SQL in the conversation. If there is no previous question to refer to, use "clarify".
D. Use "clarify" ONLY when a sensible default would probably give the wrong answer (e.g. the question
   could mean two very different analyses). Always include 2-4 clickable example questions in options.
E. Questions outside NovaPharma sales analytics (weather, general knowledge, jokes): action "clarify",
   briefly say what you can help with, and give example questions as options.

SAFETY AND SCOPE RULES:
- Query only sales, organizations, products, zip_territory. Never reference main.*, sqlite_master, or users.
- Do NOT add filters for the user's own territory or region; the tables are already filtered to their scope.
  If they ask about data outside their scope (e.g. a RAM asks to compare all territories), write the query
  normally; results will contain only their scope. Say so in the summary.
- If the user has NO pricing access and asks about revenue in dollars, WAC, price, or $: action = "refuse",
  and the message offers the same analysis in pack units or equivalents. Never reference wac for them.
- Follow-ups ("break that down by quarter", "exclude 340B") modify the previous query in the conversation.
"""


def _system_prompt(user, periods):
    """(static, dynamic). Static = rules + reference data + docs, identical every call -> cacheable.
    Dynamic = this user's schema view, scope, and periods -> goes LAST."""
    static = (PLAN_RULES
              + "\n\nREFERENCE DATA: regions and their territories\n" + db.get_reference()
              + "\n\nBUSINESS DOCUMENTS\n" + domain_knowledge())
    return (static, _schema(user, periods))


def plan(user, messages, periods):
    return _chat_json(_system_prompt(user, periods), messages)


def resolve(user, messages, periods, prior_plan, lookup, matches_by_term, note=""):
    """Second (and last) hop: continue after a name lookup, resolving EVERY search term
    together in one call. `matches_by_term` = {term: [matches]}.
    `note` explains WHY the lookup happened (e.g. the backstop: a guessed name returned 0 rows)."""
    kind = lookup.get("kind", "organization")
    convo = list(messages) + [
        {"role": "assistant", "content": json.dumps(prior_plan)},
        {"role": "user", "content": (
            (f"{note}\n\n" if note else "")
            + f"Lookup results for {kind} names within this user's access scope, "
            f"grouped by search term:\n{json.dumps(matches_by_term, default=str)}\n\n"
            "Continue. Do NOT request another lookup. Resolve EACH term independently:\n"
            "- A term with exactly one clear match: use its EXACT stored name on the right column "
            "(grandparent_org_name for a health system, parent_org_name for a parent group, "
            "org_name for a facility).\n"
            "- If ANY term matches several distinct entities and the question is about one of them: "
            "action 'clarify', naming the options for that term, with clickable options that use "
            "exact names (e.g. 'Compare Memorial Health System vs Lakeshore Medical Center').\n"
            "- The user clearly wants every match for a term (e.g. 'all Memorial accounts'): "
            "include them all.\n"
            "- If ANY term has no match: action 'clarify' saying which name was not found within "
            "their access, with rephrasing options.\n"
            "- If every term resolves: write ONE final query covering all of them.")},
    ]
    return _chat_json(_system_prompt(user, periods), convo)


def repair(user, messages, periods, failures):
    """Retry with the FULL history of failed attempts."""
    convo = list(messages)
    for i, f in enumerate(failures, 1):
        convo.append({"role": "assistant", "content": json.dumps(f["plan"])})
        hint = ("Earlier attempts also failed. Try a DIFFERENT approach, "
                "not a small tweak of the same query. ") if i > 1 else ""
        convo.append({"role": "user", "content": (
            f"Attempt {i} failed with this error:\n{f['error']}\n\n{hint}"
            "Fix the query and return the same JSON format. If the question cannot be "
            "answered with the available data, return action 'refuse' and explain why.")})
    return _chat_json(_system_prompt(user, periods), convo)


def explain_failure(user, messages, periods, reason):
    """Last resort before giving up: turn a technical failure into a plain-language
    explanation plus answerable alternative questions. The model sees the technical
    reason; the USER never does."""
    note = (
        "\n\n[INTERNAL NOTE, not written by the user] The question above could not be answered. "
        f"Technical reason: {reason}\n"
        "Write a short, friendly explanation (1-2 sentences) for a non-technical sales user of what "
        "went wrong, in plain business language. Do NOT mention SQL, queries, tables, columns, error "
        "messages, code, or AI models. Then suggest 2-4 alternative questions that CAN be answered "
        "with this data and stay as close as possible to what they wanted (for example: a narrower "
        "time period, a single product, account level instead of facility level, or splitting a "
        "complex comparison into simpler parts).\n"
        'Respond with ONLY JSON: {"message": "<explanation>", "options": ["<question>", "..."]}'
    )
    convo = list(messages[:-1]) + [
        {"role": "user", "content": messages[-1]["content"] + note}]
    return _chat_json(_system_prompt(user, periods), convo)


# ---------------------------------------------------------------------------
# Call 2: grounded answer. Output is tidied and verified in code (pipeline.py).
# ---------------------------------------------------------------------------

ANSWER_RULES = """You are a commercial analytics assistant for NovaPharma. Answer the user's question
using ONLY the query result provided.

NUMBERS (strict; your answer is automatically checked against the result):
- State only numbers that appear in the result rows, EXACTLY as given. No rounding to
  "about 213,000", no estimates. Do NOT add, subtract, average, or compute percentages yourself.
  If a total or share is not in the rows, do not state one.
- Whole numbers without decimals (2,563 not 2,563.0). Thousands separators. Percentages as given.

FORMAT:
- 1-3 plain sentences. No headings, no bullet lists, no tables, no bold: the full result table
  is shown to the user directly below your answer. Highlight only the most important values.
- Plain business language. Never mention SQL, tables, or column names.
- Be precise when grouping values: only describe a set as "ranging from X to Y" if every member
  of that set actually falls in that range.

CONTEXT:
- If the result is empty, say no matching data was found within their access scope.
- Mention any assumption from the query description (e.g. which time period was used).
- If the result includes the current month or quarter, note it is partial (data through the
  data_through date). NEVER describe a partial period as a decline or growth versus a complete
  period; say it is not yet comparable.
- If a market share is above 100%, say plainly that the third-party market data in this dataset
  does not appear to include NovaPharma's own volume, so share is overstated against the documented
  formula. Do not speculate about other causes.
"""


def _answer_messages(question, summary, result, periods, feedback=None):
    payload = {
        "question": question,
        "query_description": summary,
        "data_through": periods["data_through"],
        "columns": result["columns"],
        "rows": result["rows"][:30],
        "total_rows": len(result["rows"]),
        "truncated": result["truncated"],
    }
    if feedback:
        payload["correction"] = (
            f"Your previous answer stated these numbers, which do NOT appear in the result: "
            f"{feedback}. Rewrite the answer using only values present in the rows, exactly "
            "as given. Do not round, estimate, or compute totals or percentages.")
    return [{"role": "user", "content": json.dumps(payload, default=str)}]


def answer(question, summary, result, periods, feedback=None):
    return _chat(ANSWER_RULES, _answer_messages(question, summary, result, periods, feedback),
                 max_tokens=600, models=ANSWER_MODELS).strip()


def answer_stream(question, summary, result, periods):
    yield from _stream(ANSWER_RULES, _answer_messages(question, summary, result, periods),
                       max_tokens=600, models=ANSWER_MODELS)


# ---------------------------------------------------------------------------
# Explaining a query the user explicitly asked to see
# ---------------------------------------------------------------------------

EXPLAIN_SQL_RULES = """You explain a SQL query to a non-technical pharmaceutical sales user.
You are given the exact query that was run and a plain-language description of what it answered.
Write 3-5 short bullet points, each starting with "- ", describing in business terms what the query
does, step by step: which data it counts (and what it excludes, e.g. free drug or competitor products),
how accounts, products or territories are grouped, the time window, any calculation (share, growth,
totals), and how results are sorted or limited. End with a bullet noting that results are
automatically limited to the user's access. Describe ONLY what is in the query; do not invent
anything. Plain text only: no headings, no bold, no code."""


def explain_sql(sql, summary):
    """Plain-English, step-by-step explanation of a query the user explicitly asked to see."""
    payload = json.dumps({"sql": sql, "description": summary or ""})
    return _chat(EXPLAIN_SQL_RULES, [{"role": "user", "content": payload}],
                 max_tokens=400, models=ANSWER_MODELS).strip()