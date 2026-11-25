"""Extract schema.json from SQL Server in 3 bulk queries (the original script ran 3 queries PER table).

Usage:  python -m scripts.extract_schema            (reads DB_* from .env)

Sample rows are NOT extracted by default: they are not used by the pipeline and tables like
User_Mst / OTP_Request hold personal data. Set SAMPLE_ROWS=3 only if you really need them.
"""
import json
import os
from collections import defaultdict

import pyodbc

from app.config import get_settings

s = get_settings()
conn = pyodbc.connect(s.odbc_conn_str(), timeout=s.db_login_timeout_sec)
cur = conn.cursor()

cur.execute("""SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
               WHERE TABLE_TYPE='BASE TABLE' ORDER BY 1,2""")
tables = [f"{r[0]}.{r[1]}" for r in cur.fetchall()]

cols = defaultdict(list)
cur.execute("""SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH
               FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION""")
for sch, tbl, name, typ, nul, ln in cur.fetchall():
    cols[f"{sch}.{tbl}"].append({"name": name, "type": typ, "nullable": nul == "YES", "max_length": ln})

pks = defaultdict(list)
cur.execute("""SELECT KU.TABLE_SCHEMA, KU.TABLE_NAME, KU.COLUMN_NAME
               FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS TC
               JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE KU
                 ON TC.CONSTRAINT_NAME=KU.CONSTRAINT_NAME AND TC.TABLE_SCHEMA=KU.TABLE_SCHEMA
               WHERE TC.CONSTRAINT_TYPE='PRIMARY KEY' ORDER BY KU.ORDINAL_POSITION""")
for sch, tbl, c in cur.fetchall():
    pks[f"{sch}.{tbl}"].append(c)

fks = defaultdict(list)
cur.execute("""SELECT OBJECT_SCHEMA_NAME(fk.parent_object_id), OBJECT_NAME(fk.parent_object_id), c1.name,
                      OBJECT_SCHEMA_NAME(fk.referenced_object_id), OBJECT_NAME(fk.referenced_object_id), c2.name
               FROM sys.foreign_keys fk
               JOIN sys.foreign_key_columns fkc ON fk.object_id=fkc.constraint_object_id
               JOIN sys.columns c1 ON fkc.parent_object_id=c1.object_id AND fkc.parent_column_id=c1.column_id
               JOIN sys.columns c2 ON fkc.referenced_object_id=c2.object_id AND fkc.referenced_column_id=c2.column_id""")
for ps, pt, pc, rs, rt, rc in cur.fetchall():
    fks[f"{ps}.{pt}"].append({"column": pc, "references_table": f"{rs}.{rt}", "references_column": rc})

doc = [{"table": t, "description": "", "columns": cols[t], "primary_keys": pks[t],
        "foreign_keys": fks[t], "sample_rows": []} for t in tables]
os.makedirs(os.path.dirname(s.schema_file) or ".", exist_ok=True)
with open(s.schema_file, "w", encoding="utf-8") as f:
    json.dump(doc, f, indent=2)
print(f"Wrote {len(doc)} tables to {s.schema_file}. Add descriptions in {s.descriptions_file}, then run: python -m scripts.build_index")
