"""Loads schema.json (+ optional human descriptions) into an in-memory model."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path


def norm_name(name: str) -> str:
    """'[dbo].[User_Mst]' / 'User_Mst' / 'dbo.user_mst' -> 'dbo.user_mst'."""
    n = name.replace("[", "").replace("]", "").replace('"', "").strip().lower()
    return n if "." in n else f"dbo.{n}"


def split_identifier(s: str) -> list[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return [t for t in re.split(r"[^A-Za-z0-9]+", s.lower()) if t]


@dataclass
class Table:
    name: str
    columns: list[dict]
    primary_keys: list[str] = field(default_factory=list)
    foreign_keys: list[dict] = field(default_factory=list)
    description: str = ""

    @property
    def key(self) -> str:
        return norm_name(self.name)

    @property
    def column_names(self) -> set[str]:
        return {c["name"].lower() for c in self.columns}


class SchemaStore:
    def __init__(self, schema_file: str, descriptions_file: str | None = None):
        raw = Path(schema_file).read_bytes()
        descs: dict[str, str] = {}
        if descriptions_file and Path(descriptions_file).exists():
            descs = {
                norm_name(k): v
                for k, v in json.loads(Path(descriptions_file).read_text("utf-8")).items()
            }
        self.tables: dict[str, Table] = {}
        for t in json.loads(raw):
            tbl = Table(
                name=t["table"],
                columns=t["columns"],
                primary_keys=t.get("primary_keys", []),
                foreign_keys=t.get("foreign_keys", []),
                description=descs.get(norm_name(t["table"])) or t.get("description", ""),
            )
            self.tables[tbl.key] = tbl
        self.version = hashlib.sha256(
            raw + json.dumps(descs, sort_keys=True).encode()
        ).hexdigest()[:12]

    def get(self, name: str) -> Table | None:
        return self.tables.get(norm_name(name))

    def columns_map(self) -> dict[str, set[str]]:
        return {k: t.column_names for k, t in self.tables.items()}

    def parents(self, key: str) -> list[str]:
        t = self.tables.get(key)
        return [norm_name(fk["references_table"]) for fk in (t.foreign_keys if t else [])]

    def embedding_text(self, t: Table) -> str:
        cols = ", ".join(" ".join(split_identifier(c["name"])) for c in t.columns)
        parts = [f"Table {' '.join(split_identifier(t.name.split('.')[-1]))}."]
        if t.description:
            parts.append(t.description)
        parts.append(f"Columns: {cols}.")
        refs = {fk["references_table"].split(".")[-1] for fk in t.foreign_keys}
        if refs:
            parts.append(
                "Related to: " + ", ".join(" ".join(split_identifier(r)) for r in refs) + "."
            )
        return " ".join(parts)

    def render(self, keys: list[str]) -> str:
        """Compact prompt rendering: fewer tokens = faster LLM + fewer hallucinations."""
        out = []
        for k in keys:
            t = self.tables[k]
            fk_by_col = {fk["column"]: fk for fk in t.foreign_keys}
            cols = []
            for c in t.columns:
                s = f"{c['name']} {c['type']}"
                if c["name"] in t.primary_keys:
                    s += " PK"
                if c["name"] in fk_by_col:
                    fk = fk_by_col[c["name"]]
                    s += f" ->{fk['references_table']}.{fk['references_column']}"
                cols.append(s)
            head = t.name + (f"  -- {t.description}" if t.description else "")
            out.append(f"{head}\n  ({', '.join(cols)})")
        return "\n".join(out)
