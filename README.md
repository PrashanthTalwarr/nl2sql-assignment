# NL-to-SQL Commercial Analytics Assistant

A conversational AI agent that answers plain-English questions about pharmaceutical commercial
sales data (40,000 organizations, 2,000,000 sales rows), with **role-based access control enforced
in the database layer** and **every number in every answer verified against the query result**.

**▶ Live app: http://35.90.30.116**

> Built as a take-home assessment for Avyxa. The UI is branded "Avyxa Pharma Analytics" for the
> assessment; it is not an official Avyxa product. The data describes a fictional company, NovaPharma.

## Try it in 30 seconds

1. Open the live app and pick a role: **Executive**, **Director** (Northeast), or **RAM** (New York Metro).
2. Ask **"What are our total sales?"**
3. Sign out, pick a different role, and ask the same question.

| Role | What they see |
|---|---|
| Executive | Company-wide units **and** WAC dollars |
| Director | Northeast region only, units only |
| RAM | New York Metro territory only, units only |

**Other things to try:** "How is Memorial doing?" (ambiguous name → real matching accounts) ·
"What is our market share for Zenovax in the Docetaxel market?" · "Top 5 accounts this quarter" →
"Now exclude 340B facilities" (multi-turn) · "Show me the SQL" (SQL is shown only on request) ·
as a RAM: "Compare all territories" or "What is my revenue in dollars?"

## Results

| | |
|---|---|
| **Evaluation** | **36/36** cases pass (6 categories, 5 rubric dimensions, 18 with execution accuracy against reference SQL). The first run scored 30/36 and found real defects, which were fixed |
| **Security** | 0 scope or pricing leaks; 13/13 deterministic security checks; all 6 scenarios in `docs/security_model.md` covered |
| **Other suites** | API 12/12 · name-lookup backstop 15/15 · SQL-on-request 12/12 |
| **Latency** | Median 8.4s, p95 16.2s; the result table appears before the answer text |

Details: [`TEST_RESULTS.md`](TEST_RESULTS.md) · full per-case evidence: [`evals/results_final.md`](evals/results_final.md)

## How it works

```
question → Plan (Claude Sonnet: JSON action + SQL)
         → [Lookup: resolve fuzzy account names in the user's scope, max 1 hop]
         → Guard + Execute (read-only SQLite, TEMP views that contain only the user's rows)
         → [Repair: up to 3 retries on SQL errors]
         → Answer (Claude Haiku, from the real rows only, streamed)
         → Verify (code checks every number against the result)
```

- **Bounded workflow agent:** the LLM makes the judgment calls; code enforces every limit (at most 7 LLM calls per question, typically 2).
- **Security never depends on the LLM:** out-of-scope rows and the `wac` column don't exist for the user's database connection.
- **Domain knowledge:** all 8 business documents in the prompt, plus explicit rules for the high-stakes definitions ("sales" = paid demand; the market share formula; account rollups).
- **Accuracy over latency over cost**, with prompt caching, streaming, and model tiering to keep latency reasonable.

Full design, trade-offs, and assumptions: **[`DESIGN.md`](DESIGN.md)**

## Repository layout

| Path | Contents |
|---|---|
| `app/` | FastAPI app: API (`main.py`), agent (`pipeline.py`), LLM layer and prompts (`llm.py`), security (`security.py`), database tools (`db.py`), number verifier (`verify.py`) |
| `static/index.html` | Chat UI (single file) |
| `docs/`, `schema/` | Business documents and data generator (provided with the assignment) |
| `scripts/` | Database loading and deterministic test suites |
| `evals/` | Evaluation dataset, DeepEval harness, report renderer, results |
| `Dockerfile`, `DEPLOY.md` | Container and step-by-step AWS deployment |

## Run locally

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python schema/generate_data.py                        # generate the CSVs (deterministic)
python scripts/load_db.py                             # build pharma.db (indexes, NULL fix, ANALYZE)
copy .env.example .env                                # then add ANTHROPIC_API_KEY and SESSION_SECRET
uvicorn app.main:app --port 8080                      # open http://localhost:8080
```

## Run the tests

```bash
python scripts/test_security.py        # 13 checks: scoping, WAC, SQL guard
python scripts/test_api.py             # 12 checks: auth, validation, rate limit
python scripts/test_backstop.py        # 15 checks: name lookup and backstop
python scripts/test_sql_request.py     # 12 checks: SQL on request
pip install -r requirements-dev.txt
python evals/run_eval.py               # 36-case evaluation (needs OPENAI_API_KEY for the judge)
python evals/render_report.py          # formatted report
```

## Deployment

Docker image on **AWS EC2**, pulled from **ECR** through an IAM role, with logs in **CloudWatch**.
See [`DEPLOY.md`](DEPLOY.md) for reproducible steps.

## Documents

| | |
|---|---|
| [`DESIGN.md`](DESIGN.md) | Architecture, agent design, security, trade-offs, assumptions |
| [`TEST_RESULTS.md`](TEST_RESULTS.md) | Test cases and results |
| [`evals/results_final.md`](evals/results_final.md) | Every eval case with generated SQL, expected vs. actual rows, and scores |
| [`DEPLOY.md`](DEPLOY.md) | AWS deployment steps |
