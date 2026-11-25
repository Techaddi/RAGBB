"""SQL Server execution (pyodbc) - run in a thread pool so the event loop never blocks."""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import decimal
import uuid
from dataclasses import dataclass

from .config import Settings


class DbError(RuntimeError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    truncated: bool


def _json_safe(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return f"<binary {len(v)} bytes>" if len(v) > 64 else base64.b64encode(v).decode()
    return str(v)


class Database:
    def __init__(self, s: Settings):
        self.s = s
        self.conn_str = s.odbc_conn_str()
        self.sem = asyncio.Semaphore(s.db_concurrency)

    def _run(self, sql: str, max_rows: int) -> QueryResult:
        try:
            import pyodbc                                   # lazy: app/tests start without an ODBC driver installed
        except ImportError as e:
            raise DbError(f"pyodbc / ODBC driver not available: {e}") from e
        pyodbc.pooling = True                               # driver-manager connection pooling (cheap reconnects)
        try:
            conn = pyodbc.connect(self.conn_str, timeout=self.s.db_login_timeout_sec, autocommit=True)
            try:
                conn.timeout = self.s.db_query_timeout_sec        # per-statement timeout
                cur = conn.cursor()
                cur.execute(sql)
                cols = [c[0] for c in cur.description] if cur.description else []
                rows = cur.fetchmany(max_rows + 1)
                truncated = len(rows) > max_rows
                rows = [[_json_safe(v) for v in r] for r in rows[:max_rows]]
                return QueryResult(cols, rows, truncated)
            finally:
                conn.close()
        except pyodbc.Error as e:
            raise DbError(str(e.args[-1] if e.args else e)[:400]) from e

    async def execute(self, sql: str, max_rows: int) -> QueryResult:
        async with self.sem:
            return await asyncio.to_thread(self._run, sql, max_rows)

    async def ping(self) -> bool:
        try:
            await self.execute("SELECT 1", 1)
            return True
        except Exception:
            return False
