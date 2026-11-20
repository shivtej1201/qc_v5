import os as _os, sys as _sys  # project root on sys.path (script lives in scripts/diagnostics)
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))
import sqlite3
import pandas as pd
conn = sqlite3.connect('audit_cache.db')
df = pd.read_sql("SELECT normalized_url, business, page_type, classification_method FROM url_results WHERE normalized_url LIKE '%bajajfinserv.in%'", conn)
print("Total bajaj URLs:", len(df))
print("Missing business:", df['business'].isna().sum())
print("Missing page_type:", df['page_type'].isna().sum())
print(df[df['business'].isna() | df['page_type'].isna()].head())
