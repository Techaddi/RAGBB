"""AST-based SQL safety + schema validation (sqlglot, T-SQL dialect).

Much stronger than keyword regexes: we parse the statement and check
 * exactly one statement, and it is a read-only query
 * no INTO / EXEC / dynamic SQL / dangerous functions / cross-db or system objects
 * every table exists (in the real schema or in the *proposed* new tables)
 * every column exists on the table it is qualified with
 * no blocked (sensitive) columns, and no SELECT * on tables that contain them
 * row cap injected as TOP n
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .schema_store import norm_name

DIALECT = "tsql"
BAD_FUNCS = ("xp_", "sp_", "openrowset", "opendatasource", "openquery", "openjson_remote", "fn_")
SYSTEM_SCHEMAS = {"sys", "information_schema", "master", "msdb", "tempdb"}
_FORBIDDEN = tuple(
    getattr(exp, n)
    for n in (
        "Insert", "Update", "Delete", "Drop", "Create", "Alter", "Merge", "Command",
        "Into", "TruncateTable", "Grant", "Revoke", "Use", "Set", "Transaction", "Commit", "Rollback",
    )
    if hasattr(exp, n)
)


class SqlRejected(ValueError):
    """Raised with a message that is safe to feed back to the LLM for self-correction."""


@dataclass
class Validated:
    sql: str
    tables: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _parse_one(sql: str) -> exp.Expression:
    try:
        stmts = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except ParseError as e:
        raise SqlRejected(f"SQL does not parse as T-SQL: {str(e)[:200]}") from e
    if len(stmts) != 1:
        raise SqlRejected("Exactly one statement is allowed.")
    return stmts[0]


def validate_select(
    sql: str,
    known: dict[str, set[str]],
    blocked_columns: set[str],
    max_rows: int,
    proposed: dict[str, set[str]] | None = None,
) -> Validated:
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise SqlRejected("Empty SQL.")
    tree = _parse_one(sql)
    if not isinstance(tree, exp.Query):
        raise SqlRejected("Only SELECT queries are allowed.")

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN):
            raise SqlRejected(f"Forbidden construct: {type(node).__name__}.")
        if isinstance(node, (exp.Anonymous,)) and node.name:
            lname = node.name.lower()
            if any(lname.startswith(b) for b in BAD_FUNCS):
                raise SqlRejected(f"Function '{node.name}' is not allowed.")

    proposed = proposed or {}
    universe = {**known, **proposed}
    cte_names = {c.alias.lower() for c in tree.find_all(exp.CTE) if c.alias}

    alias_to_key: dict[str, str] = {}
    used: list[str] = []
    unknown_tables: list[str] = []
    for t in tree.find_all(exp.Table):
        if not t.name:
            continue
        if t.catalog:
            raise SqlRejected("Cross-database references are not allowed.")
        if (t.db or "").lower() in SYSTEM_SCHEMAS:
            raise SqlRejected("System schemas/objects are not allowed.")
        if not t.db and t.name.lower() in cte_names:
            continue
        key = norm_name(f"{t.db}.{t.name}" if t.db else t.name)
        if key not in universe:
            unknown_tables.append(t.sql(dialect=DIALECT))
            continue
        alias_to_key[(t.alias or t.name).lower()] = key
        if key not in used:
            used.append(key)
    if unknown_tables:
        raise SqlRejected(
            "These tables do not exist in the schema: " + ", ".join(sorted(set(unknown_tables)))
            + ". Use only listed tables, or declare them under missing_tables."
        )

    referenced_cols = set().union(*(universe[k] for k in used)) if used else set()
    select_aliases = {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    subq_aliases = {s.alias.lower() for s in tree.find_all(exp.Subquery) if s.alias}
    bad_cols: list[str] = []
    for col in tree.find_all(exp.Column):
        name = col.name.lower()
        if not name:
            continue
        if name in blocked_columns:
            raise SqlRejected(f"Column '{col.name}' is restricted and cannot be queried.")
        q = col.table.lower() if col.table else ""
        if q:
            key = alias_to_key.get(q)
            if key is None:
                if q in cte_names or q in subq_aliases:
                    continue
                bad_cols.append(f"{col.table}.{col.name} (unknown alias)")
            elif name not in universe[key]:
                bad_cols.append(f"{col.table}.{col.name} (no such column in {key})")
        elif name not in referenced_cols and name not in select_aliases:
            bad_cols.append(col.name)
    if bad_cols:
        raise SqlRejected("Unknown columns: " + ", ".join(sorted(set(bad_cols))))

    # SELECT * on a table with sensitive columns would leak them.
    for star in tree.find_all(exp.Star):
        parent = star.parent
        tables = [alias_to_key[parent.table.lower()]] if isinstance(parent, exp.Column) and parent.table in alias_to_key \
            else used
        for k in tables:
            if universe[k] & blocked_columns:
                raise SqlRejected(f"SELECT * on {k} would expose restricted columns; list columns explicitly.")

    warnings: list[str] = []
    if isinstance(tree, exp.Select) and not tree.args.get("limit"):
        tree = tree.limit(max_rows)
        warnings.append(f"Row cap TOP {max_rows} applied.")
    return Validated(sql=tree.sql(dialect=DIALECT), tables=used, warnings=warnings)


def validate_ddl(
    statements: list[str], known: dict[str, set[str]], max_tables: int
) -> tuple[dict[str, set[str]], list[str]]:
    """Validate LLM-proposed CREATE TABLE statements. Never executed - proposal only.
    Returns ({table_key: {columns}}, [normalized ddl])."""
    if len(statements) > max_tables:
        raise SqlRejected(f"Propose at most {max_tables} new tables.")
    out: dict[str, set[str]] = {}
    ddls: list[str] = []
    for ddl in statements:
        tree = _parse_one(ddl.strip().rstrip(";"))
        if not (isinstance(tree, exp.Create) and (tree.args.get("kind") or "").upper() == "TABLE"):
            raise SqlRejected("missing_tables[].ddl must be a single CREATE TABLE statement.")
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        if not isinstance(target, exp.Table):
            raise SqlRejected("Could not read table name from CREATE TABLE.")
        key = norm_name(f"{target.db}.{target.name}" if target.db else target.name)
        if key in known:
            raise SqlRejected(f"Table {key} already exists - use it instead of proposing it.")
        cols = {c.name.lower() for c in tree.find_all(exp.ColumnDef)}
        if not cols:
            raise SqlRejected(f"CREATE TABLE {key} has no columns.")
        out[key] = cols
        ddls.append(tree.sql(dialect=DIALECT, pretty=True))
    return out, ddls
