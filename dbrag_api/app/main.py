from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from .db import Database
from .infra import PlanCache, require_admin, require_api_key
from .llm import LLMClient
from .models import QueryRequest, QueryResponse
from .pipeline import Pipeline
from .retriever import Retriever
from .schema_store import SchemaStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("dbrag")


def build_pipeline(s: Settings) -> Pipeline:
    store = SchemaStore(s.schema_file, s.descriptions_file)
    retriever = Retriever(store, s.chroma_dir)
    if not retriever.is_current():
        log.info("vector index missing/stale - rebuilding")
        retriever.build()
    return Pipeline(s, store, retriever, LLMClient(s), Database(s), PlanCache(s.plan_cache_size, s.plan_cache_ttl_sec))


def create_app(pipeline: Pipeline | None = None, settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if s.is_prod and not s.api_key_set:
            raise RuntimeError("API_KEYS must be set when ENV=production")
        app.state.pipeline = pipeline or await asyncio.to_thread(build_pipeline, s)
        asyncio.create_task(app.state.pipeline.llm.warmup())      # load model into memory before first user
        log.info("ready: %d tables, schema=%s", len(app.state.pipeline.store.tables), app.state.pipeline.store.version)
        yield
        await app.state.pipeline.llm.aclose()

    app = FastAPI(title="DB RAG Text-to-SQL API", version="1.0.0", lifespan=lifespan,
                  docs_url=None if s.is_prod else "/docs", redoc_url=None, openapi_url=None if s.is_prod else "/openapi.json")

    app.state.settings = s

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.rid = rid
        t = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled rid=%s", rid)
            response = JSONResponse({"detail": "Internal error", "request_id": rid}, status_code=500)
        response.headers["X-Request-ID"] = rid
        log.info(json.dumps({"rid": rid, "method": request.method, "path": request.url.path,
                             "status": response.status_code, "ms": int((time.perf_counter() - t) * 1000)}))
        return response

    @app.post("/v1/query", response_model=QueryResponse, dependencies=[Depends(require_api_key)])
    async def query(req: QueryRequest, request: Request):
        """Question -> SQL -> (rows). If the question needs tables that don't exist,
        returns status=needs_new_tables with proposed CREATE TABLE DDL + the SQL that would work."""
        return await request.app.state.pipeline.run(req, request.state.rid)

    @app.post("/v1/sql", response_model=QueryResponse, dependencies=[Depends(require_api_key)])
    async def sql_only(req: QueryRequest, request: Request):
        """Generate + validate SQL only (never touches the data)."""
        return await request.app.state.pipeline.run(req.model_copy(update={"execute": False, "summarize": False}), request.state.rid)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request):
        p: Pipeline = request.app.state.pipeline
        llm_ok, db_ok = await asyncio.gather(p.llm.ping(), p.db.ping())
        body = {"llm": llm_ok, "database": db_ok, "tables": len(p.store.tables), "schema_version": p.store.version,
                "plan_cache": {"hits": p.cache.hits, "misses": p.cache.misses, "size": len(p.cache.c)}}
        return JSONResponse(body, status_code=200 if llm_ok and db_ok else 503)

    @app.post("/v1/admin/reindex", dependencies=[Depends(require_admin)])
    async def reindex(request: Request):
        """Reload schema.json + descriptions and rebuild the vector index (zero-downtime swap)."""
        p: Pipeline = request.app.state.pipeline
        store = SchemaStore(s.schema_file, s.descriptions_file)
        retr = Retriever(store, s.chroma_dir, embedder=p.retriever.embedder)
        n = await asyncio.to_thread(retr.build)
        p.store, p.retriever = store, retr
        p.cache.clear()
        return {"indexed_tables": n, "schema_version": store.version}

    return app


def get_app() -> FastAPI:          # run with: uvicorn app.main:get_app --factory
    return create_app()
