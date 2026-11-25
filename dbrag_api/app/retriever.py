"""Hybrid table retrieval: vector search + lexical match, fused with RRF, then FK expansion.

Why hybrid: embeddings catch meaning ("members" -> User_Mst), the lexical pass catches
exact identifiers ("Reel_View", "pincode") that small embedding models blur.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

import chromadb

from .schema_store import SchemaStore, split_identifier

log = logging.getLogger("dbrag.retriever")
COLLECTION = "schema_tables"
STOP = {"the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "by", "with", "me", "show", "list",
        "get", "give", "how", "many", "much", "what", "which", "who", "is", "are", "all", "per", "each", "from"}


def _default_embedder():
    # all-MiniLM-L6-v2 via ONNX: same model as the original project, no torch dependency, faster cold start.
    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
    return ONNXMiniLM_L6_V2()


class Retriever:
    def __init__(self, store: SchemaStore, chroma_dir: str, embedder=None):
        self.store = store
        self.chroma_dir = chroma_dir
        self.embedder = embedder or _default_embedder()
        self.client = chromadb.PersistentClient(path=chroma_dir)
        self.collection = self.client.get_or_create_collection(
            COLLECTION, embedding_function=self.embedder, metadata={"hnsw:space": "cosine"}
        )
        self._lex = self._build_lexical()

    # ---------- indexing ----------
    def _build_lexical(self):
        idx = {}
        for key, t in self.store.tables.items():
            name_tokens = set(split_identifier(t.name.split(".")[-1]))
            col_tokens = {tok for c in t.columns for tok in split_identifier(c["name"])}
            desc_tokens = set(split_identifier(t.description))
            idx[key] = (name_tokens, col_tokens, desc_tokens)
        return idx

    def is_current(self) -> bool:
        meta = self.collection.metadata or {}
        return self.collection.count() == len(self.store.tables) and meta.get("schema_version") == self.store.version

    def build(self) -> int:
        try:
            self.client.delete_collection(COLLECTION)
        except Exception:
            pass
        self.collection = self.client.create_collection(
            COLLECTION, embedding_function=self.embedder,
            metadata={"hnsw:space": "cosine", "schema_version": self.store.version},
        )
        keys = list(self.store.tables)
        self.collection.add(
            ids=keys,
            documents=[self.store.embedding_text(self.store.tables[k]) for k in keys],
            metadatas=[{"table": self.store.tables[k].name} for k in keys],
        )
        self._lex = self._build_lexical()
        log.info("indexed %d tables (schema %s)", len(keys), self.store.version)
        return len(keys)

    # ---------- search ----------
    def _vector(self, question: str, n: int) -> list[str]:
        r = self.collection.query(query_texts=[question], n_results=min(n, self.collection.count()))
        return r["ids"][0]

    def _lexical(self, question: str) -> list[str]:
        toks = {t for t in split_identifier(question) if t not in STOP and len(t) > 1}
        # crude singularisation so "reels"/"users"/"likes" match reel/user/like
        toks |= {t[:-1] for t in toks if t.endswith("s") and len(t) > 3}
        scored = []
        for key, (name_t, col_t, desc_t) in self._lex.items():
            s = 3 * len(toks & name_t) + 1.0 * len(toks & col_t) + 0.5 * len(toks & desc_t)
            if s > 0:
                scored.append((s, key))
        return [k for _, k in sorted(scored, reverse=True)]

    def search(self, question: str, k: int = 6) -> list[str]:
        vec = self._vector(question, k * 2)
        lex = self._lexical(question)[: k * 2]
        score: dict[str, float] = {}
        for ranking in (vec, lex):                      # Reciprocal Rank Fusion
            for rank, key in enumerate(ranking):
                score[key] = score.get(key, 0.0) + 1.0 / (60 + rank)
        top = [k_ for k_, _ in sorted(score.items(), key=lambda x: -x[1])][:k]
        # FK expansion: pull in referenced parent tables so JOINs are possible (cap +3)
        extra = []
        for key in top:
            for p in self.store.parents(key):
                if p in self.store.tables and p not in top and p not in extra:
                    extra.append(p)
        return top + extra[:3]
