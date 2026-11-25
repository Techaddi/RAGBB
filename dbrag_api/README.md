# DB-RAG Text-to-SQL API

FastAPI service for your .NET Core app. Natural-language question -> relevant tables (RAG) -> T-SQL -> validated -> executed read-only -> JSON.
Built from your `DBRAG` scripts (same schema.json, same Ollama `qwen2.5-coder` model, same MiniLM embeddings).

## Quick start
```bash
cp .env.example .env            # set API_KEYS, DB_*, LLM_*
pip install -r requirements.txt
python -m scripts.build_index   # one-off (re-run after schema changes, or POST /v1/admin/reindex)
uvicorn app.main:get_app --factory --port 8000
# Docker: docker compose up -d --build && docker compose exec ollama ollama pull qwen2.5-coder:7b
```
Tests: `pip install -r requirements-dev.txt && pytest`

## API
`POST /v1/query`  header `X-API-Key`
```json
{ "question": "top 5 cities by number of news posts", "execute": true, "max_rows": 100, "summarize": false }
```
Response `status`:
* `ok` - SQL generated, validated, (executed) -> `columns`, `rows`, `truncated`, `answer` (if `summarize`)
* `needs_new_tables` - the question needs data your DB doesn't have. You get `missing_tables[]` (each with a validated
  `CREATE TABLE` DDL) **plus** the `sql` that would answer the question once they exist. Nothing is executed and DDL is never run.
* `error` - could not produce safe, valid SQL after `MAX_ATTEMPTS`

Other: `POST /v1/sql` (generate only), `GET /healthz`, `GET /readyz` (LLM + DB check), `POST /v1/admin/reindex` (admin key).
.NET client + DI + Polly resilience: `dotnet/DbRag.Client/`.

## What changed vs. your scripts (and why it is faster / safer)
| Area | Before | Now |
|---|---|---|
| Latency | embedding model + Chroma reloaded per call, `requests` blocking | loaded once at startup, async httpx, model kept warm (`keep_alive`), **plan cache** (repeat questions skip retrieval + LLM) |
| Retrieval | vector top-5 | vector + lexical (identifiers) fused with RRF, + FK parent expansion so JOINs work; table descriptions in `data/table_descriptions.json` |
| Prompt | one-shot, plain-text SQL | compact schema (FKs inline), few-shot, JSON output, T-SQL rules |
| Safety | keyword regex | **sqlglot AST**: single SELECT only, no INTO/EXEC/system schemas/cross-DB, tables + columns must exist, sensitive columns blocked, TOP cap, query timeout |
| Accuracy | single try | self-correction loop: validator or SQL Server error is fed back to the LLM (up to `MAX_ATTEMPTS`) |
| Missing tables | invented/failed | explicit `needs_new_tables` flow with validated DDL proposal |
| Ops | CLI | API key auth, rate limit, request-id, JSON logs, health/readiness, Docker (ODBC 18, non-root, gunicorn), tests |

## Production checklist
1. **DB login must be read-only** (`db_datareader`, deny on sensitive tables). The AST guard is defence in depth, not a substitute.
2. Windows auth only works on a Windows host. In Docker/Linux use a SQL login (`DB_USER`/`DB_PASSWORD`) from a secret store.
3. Put the API on a private network behind your .NET app; keep `ENV=production` (docs disabled, API_KEYS required).
4. LLM is the bottleneck: use a GPU, or a larger model via vLLM (`LLM_PROVIDER=openai`). `qwen2.5-coder:7b` is a minimum; 14B/32B is noticeably more accurate on joins.
5. Multiple replicas: rate limiter and plan cache are per-process; move them to Redis if you need global limits.
6. `data/schema.json` from your upload contained sample rows incl. personal data (mobiles etc.). The new extractor skips samples by default; regenerate and delete the old file.
7. Edit `data/table_descriptions.json` - good descriptions are the single biggest retrieval-accuracy lever. I wrote them from table/column names; please correct them (e.g. the difference between `Ad_Post` and `Advertisements_Post`, `Payment_Txn` and `Order_Payment`).
8. Applying proposed DDL should stay a human/migration step (DBA review -> migration -> `scripts.extract_schema` -> reindex).
