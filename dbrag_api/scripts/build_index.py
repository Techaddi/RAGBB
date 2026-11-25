"""Build (or rebuild) the Chroma index.  python -m scripts.build_index [--if-missing]"""
import sys

from app.config import get_settings
from app.retriever import Retriever
from app.schema_store import SchemaStore

s = get_settings()
store = SchemaStore(s.schema_file, s.descriptions_file)
r = Retriever(store, s.chroma_dir)
if "--if-missing" in sys.argv and r.is_current():
    print(f"Index up to date ({len(store.tables)} tables).")
else:
    print(f"Indexed {r.build()} tables (schema {store.version}).")
