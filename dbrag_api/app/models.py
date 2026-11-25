from typing import Any, Literal
from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=500)
    execute: bool = Field(True, description="Run the SQL and return rows. False = only generate SQL.")
    max_rows: int | None = Field(None, ge=1, le=1000)
    summarize: bool = Field(False, description="Add a short natural-language answer (one extra LLM call).")


class MissingTable(BaseModel):
    name: str
    purpose: str = ""
    ddl: str


class QueryResponse(BaseModel):
    request_id: str
    question: str
    status: Literal["ok", "needs_new_tables", "error"]
    sql: str | None = None
    tables_used: list[str] = []
    missing_tables: list[MissingTable] = []
    executed: bool = False
    columns: list[str] = []
    rows: list[list[Any]] = []
    row_count: int = 0
    truncated: bool = False
    answer: str | None = None
    notes: str | None = None
    warnings: list[str] = []
    cached_plan: bool = False
    attempts: int = 0
    timings_ms: dict[str, int] = {}
    error: str | None = None
