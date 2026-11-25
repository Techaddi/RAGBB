"""The text-to-SQL pipeline: retrieve -> generate -> validate -> (execute) -> self-correct."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from .config import Settings
from .db import Database, DbError
from .infra import PlanCache, normalize_question
from .llm import LLMClient, LLMError, extract_json
from .models import MissingTable, QueryRequest, QueryResponse
from .retriever import Retriever
from .schema_store import SchemaStore
from .sql_guard import SqlRejected, validate_ddl, validate_select

log = logging.getLogger("dbrag.pipeline")

SYSTEM_PROMPT = """You are a senior Microsoft SQL Server (T-SQL) engineer. Turn the user's question into ONE read-only SELECT query.

Reply with JSON only, exactly this shape:
{"sql": "<T-SQL SELECT>", "missing_tables": [{"name": "dbo.TableName", "purpose": "<why>", "ddl": "CREATE TABLE dbo.TableName (...)"}], "notes": "<one short sentence>"}

Rules:
1. Use ONLY the tables and columns listed in SCHEMA. Never invent a column. Columns marked "->" are foreign keys: use them for JOINs.
2. If the question needs data that NO table/column in SCHEMA can provide, do not guess. Propose the minimal new table(s) in "missing_tables" (full CREATE TABLE: primary key, sensible types, foreign keys to existing tables) and write "sql" against those proposed tables. Every table used in "sql" must be in SCHEMA or in "missing_tables".
3. If everything needed exists, "missing_tables" must be [].
4. T-SQL only: TOP n (never LIMIT), GETDATE(), DATEADD/DATEDIFF, ISNULL. Prefer explicit column lists over SELECT *.
5. Never read secret columns (passwords, OTP/token hashes, FCM tokens).
6. For "how many / total / average / top / per" questions use COUNT/SUM/AVG with GROUP BY and ORDER BY.
7. "sql" must be a single statement. No comments, no semicolons chains, no variables."""

EXAMPLE_USER = """SCHEMA:
dbo.Orders (order_id int PK, customer_id int ->dbo.Customers.customer_id, total money)
dbo.Customers (customer_id int PK, name nvarchar)

QUESTION: top 3 customers by total spend"""
EXAMPLE_ANSWER = '{"sql": "SELECT TOP 3 c.name, SUM(o.total) AS total_spend FROM dbo.Orders o JOIN dbo.Customers c ON c.customer_id = o.customer_id GROUP BY c.name ORDER BY total_spend DESC", "missing_tables": [], "notes": "Sum of orders per customer."}'


@dataclass
class Plan:
    sql: str
    tables: list[str]
    missing: list[MissingTable] = field(default_factory=list)
    notes: str = ""
    warnings: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, s: Settings, store: SchemaStore, retriever: Retriever,
                 llm: LLMClient, db: Database, cache: PlanCache):
        self.s, self.store, self.retriever, self.llm, self.db, self.cache = s, store, retriever, llm, db, cache

    async def run(self, req: QueryRequest, request_id: str) -> QueryResponse:
        import asyncio
        t0 = time.perf_counter()
        timings: dict[str, int] = {}
        max_rows = min(req.max_rows or self.s.max_rows, 1000)
        res = QueryResponse(request_id=request_id, question=req.question, status="error")
        key = (self.store.version, normalize_question(req.question), max_rows)

        plan: Plan | None = self.cache.get(key)
        feedback: list[dict] = []
        schema_text = ""
        if plan:
            res.cached_plan = True
        else:
            t = time.perf_counter()
            tables = await asyncio.to_thread(self.retriever.search, req.question, self.s.top_k_tables)
            schema_text = self.store.render(tables)
            timings["retrieve"] = int((time.perf_counter() - t) * 1000)

        last_error = "unknown error"
        for attempt in range(1, self.s.max_attempts + 1):
            res.attempts = attempt
            try:
                if plan is None:
                    t = time.perf_counter()
                    plan = await self._generate(req.question, schema_text, feedback, max_rows)
                    timings["generate"] = timings.get("generate", 0) + int((time.perf_counter() - t) * 1000)

                res.sql, res.tables_used = plan.sql, plan.tables
                res.missing_tables, res.notes, res.warnings = plan.missing, plan.notes, plan.warnings

                if plan.missing:                       # proposal only - DDL is never executed
                    res.status = "needs_new_tables"
                    self.cache.set(key, plan)
                    break
                if not req.execute:
                    res.status = "ok"
                    self.cache.set(key, plan)
                    break

                t = time.perf_counter()
                qr = await self.db.execute(plan.sql, max_rows)
                timings["db"] = int((time.perf_counter() - t) * 1000)
                res.columns, res.rows, res.truncated = qr.columns, qr.rows, qr.truncated
                res.row_count, res.executed, res.status = len(qr.rows), True, "ok"
                self.cache.set(key, plan)
                break
            except (SqlRejected, LLMError, DbError) as e:
                last_error = str(e)
                log.warning("attempt %d failed rid=%s: %s", attempt, request_id, last_error)
                if res.cached_plan:                    # stale cached plan (e.g. schema drift) -> regenerate
                    res.cached_plan = False
                    plan = None
                    tables = await asyncio.to_thread(self.retriever.search, req.question, self.s.top_k_tables)
                    schema_text = self.store.render(tables)
                    continue
                if isinstance(e, LLMError) and "request failed" in last_error:
                    break                              # LLM unreachable: retrying immediately will not help
                if plan is not None:
                    feedback = [
                        {"role": "assistant", "content": json.dumps({"sql": plan.sql})},
                        {"role": "user", "content": f"That was rejected: {last_error}\nReturn corrected JSON."},
                    ]
                else:
                    feedback = [{"role": "user", "content": f"Your previous reply was unusable: {last_error}\nReturn valid JSON only."}]
                plan = None

        if res.status == "error":
            res.error = last_error
        elif req.summarize and res.executed and res.rows:
            t = time.perf_counter()
            res.answer = await self._summarize(req.question, res.columns, res.rows)
            timings["summarize"] = int((time.perf_counter() - t) * 1000)

        timings["total"] = int((time.perf_counter() - t0) * 1000)
        res.timings_ms = timings
        return res

    async def _generate(self, question: str, schema_text: str, feedback: list[dict], max_rows: int) -> Plan:
        messages = [
            {"role": "user", "content": EXAMPLE_USER},
            {"role": "assistant", "content": EXAMPLE_ANSWER},
            {"role": "user", "content": f"SCHEMA:\n{schema_text}\n\nQUESTION: {question}"},
            *feedback,
        ]
        raw = await self.llm.chat(SYSTEM_PROMPT, messages)
        data = extract_json(raw)
        sql = (data.get("sql") or "").strip()
        missing_raw = data.get("missing_tables") or []
        if not isinstance(missing_raw, list):
            raise SqlRejected("missing_tables must be a list.")

        proposed: dict[str, set[str]] = {}
        missing: list[MissingTable] = []
        if missing_raw:
            ddls = [str(m.get("ddl", "")) for m in missing_raw if isinstance(m, dict)]
            proposed, clean = validate_ddl(ddls, self.store.columns_map(), self.s.max_new_tables)
            for m, ddl in zip(missing_raw, clean):
                missing.append(MissingTable(name=str(m.get("name", "")), purpose=str(m.get("purpose", "")), ddl=ddl))

        v = validate_select(sql, self.store.columns_map(), self.s.blocked_column_set, max_rows, proposed)
        return Plan(sql=v.sql, tables=v.tables, missing=missing, notes=str(data.get("notes", ""))[:300], warnings=v.warnings)

    async def _summarize(self, question: str, columns: list[str], rows: list[list]) -> str | None:
        sample = json.dumps({"columns": columns, "rows": rows[:25]}, default=str)[:4000]
        try:
            return (await self.llm.chat(
                "Answer the question in 1-3 plain sentences using ONLY the data given. Do not mention SQL.",
                [{"role": "user", "content": f"Question: {question}\nData: {sample}"}],
                json_mode=False, max_tokens=200,
            )).strip()
        except LLMError:
            return None
