import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import DbError, QueryResult
from app.infra import PlanCache
from app.main import create_app
from app.pipeline import Pipeline
from app.retriever import Retriever
from app.schema_store import SchemaStore


class FakeEmbedder:
    """Deterministic bag-of-words hashing embedder so tests need no model download."""
    def __call__(self, input):
        out = []
        for text in input:
            v = [0.0] * 128
            for w in text.lower().replace(",", " ").replace(".", " ").split():
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 128] += 1
            n = sum(x * x for x in v) ** 0.5 or 1
            out.append([x / n for x in v])
        return out

    @staticmethod
    def name(): return "fake"
    def embed_query(self, input): return self(input)
    def embed_documents(self, input): return self(input)
    def is_legacy(self): return False
    def default_space(self): return "cosine"
    def supported_spaces(self): return ["cosine"]
    def get_config(self): return {}
    def max_tokens(self): return 512


class FakeLLM:
    def __init__(self, replies): self.replies, self.calls = list(replies), []
    async def chat(self, system, messages, json_mode=True, max_tokens=None):
        self.calls.append(messages)
        r = self.replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r)
    async def ping(self): return True
    async def warmup(self): pass
    async def aclose(self): pass


class FakeDB:
    def __init__(self, fail_first=False): self.n, self.fail_first = 0, fail_first
    async def execute(self, sql, max_rows):
        self.n += 1
        if self.fail_first and self.n == 1:
            raise DbError("Invalid column name 'x'")
        return QueryResult(["total"], [[42]], False)
    async def ping(self): return True


def make_client(tmp_path, replies, fail_first=False):
    s = Settings(env="development", api_keys="k1", schema_file="data/schema.json",
                 descriptions_file="data/table_descriptions.json", chroma_dir=str(tmp_path / "c"))
    store = SchemaStore(s.schema_file, s.descriptions_file)
    retr = Retriever(store, s.chroma_dir, embedder=FakeEmbedder())
    retr.build()
    llm, db = FakeLLM(replies), FakeDB(fail_first)
    p = Pipeline(s, store, retr, llm, db, PlanCache(100, 60))
    return TestClient(create_app(pipeline=p, settings=s)), llm, db


H = {"X-API-Key": "k1"}
GOOD = {"sql": "SELECT COUNT(*) AS total FROM dbo.News_Post", "missing_tables": [], "notes": "count"}


def test_auth_required(tmp_path):
    with make_client(tmp_path, [GOOD])[0] as c:
        assert c.post("/v1/query", json={"question": "how many news posts"}).status_code == 401


def test_ok_and_plan_cache(tmp_path):
    c, llm, db = make_client(tmp_path, [GOOD])
    with c:
        r1 = c.post("/v1/query", headers=H, json={"question": "How many news posts?"}).json()
        r2 = c.post("/v1/query", headers=H, json={"question": "how many news posts"}).json()
    assert r1["status"] == "ok" and r1["rows"] == [[42]] and r1["executed"] and not r1["cached_plan"]
    assert r2["cached_plan"] and len(llm.calls) == 1 and db.n == 2        # LLM skipped, data re-queried


def test_retrieval_picks_relevant_tables(tmp_path):
    c, llm, _ = make_client(tmp_path, [GOOD])
    with c:
        c.post("/v1/query", headers=H, json={"question": "how many likes on each reel"})
    prompt = llm.calls[0][-1]["content"]
    assert "dbo.Reel_Likes" in prompt and "dbo.Reel_Post" in prompt


def test_hallucinated_table_self_corrects(tmp_path):
    bad = {"sql": "SELECT COUNT(*) FROM dbo.Posts", "missing_tables": []}
    c, llm, _ = make_client(tmp_path, [bad, GOOD])
    with c:
        r = c.post("/v1/query", headers=H, json={"question": "count posts"}).json()
    assert r["status"] == "ok" and r["attempts"] == 2
    assert "do not exist" in llm.calls[1][-1]["content"]


def test_db_error_self_corrects(tmp_path):
    c, _, db = make_client(tmp_path, [GOOD, GOOD], fail_first=True)
    with c:
        r = c.post("/v1/query", headers=H, json={"question": "count posts"}).json()
    assert r["status"] == "ok" and r["attempts"] == 2


def test_missing_table_returns_ddl_and_does_not_execute(tmp_path):
    reply = {
        "sql": "SELECT AVG(r.rating) AS avg_rating FROM dbo.Reviews r JOIN dbo.User_Mst u ON u.user_id = r.user_id",
        "missing_tables": [{"name": "dbo.Reviews", "purpose": "user ratings",
                            "ddl": "CREATE TABLE dbo.Reviews (review_id INT IDENTITY PRIMARY KEY, user_id INT NOT NULL REFERENCES dbo.User_Mst(user_id), rating TINYINT NOT NULL)"}],
        "notes": "needs reviews",
    }
    c, _, db = make_client(tmp_path, [reply])
    with c:
        r = c.post("/v1/query", headers=H, json={"question": "average review rating of users"}).json()
    assert r["status"] == "needs_new_tables" and not r["executed"] and db.n == 0
    assert r["missing_tables"][0]["ddl"].startswith("CREATE TABLE dbo.Reviews")


def test_unsafe_sql_never_reaches_db(tmp_path):
    evil = {"sql": "DROP TABLE dbo.User_Mst", "missing_tables": []}
    c, _, db = make_client(tmp_path, [evil, evil, evil])
    with c:
        r = c.post("/v1/query", headers=H, json={"question": "delete everything"}).json()
    assert r["status"] == "error" and db.n == 0


def test_sql_only_endpoint(tmp_path):
    c, _, db = make_client(tmp_path, [GOOD])
    with c:
        r = c.post("/v1/sql", headers=H, json={"question": "count posts"}).json()
    assert r["status"] == "ok" and not r["executed"] and db.n == 0 and "TOP 200" in r["sql"]
