import os as _os, sys as _sys  # project root on sys.path (script lives in scripts/diagnostics)
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))
import sqlite3
import pandas as pd
conn = sqlite3.connect('audit_cache.db')
df = pd.read_sql("SELECT count(*) FROM url_results WHERE page_type IS NOT NULL", conn)
print("Classified URLs:", df.iloc[0,0])

df2 = pd.read_sql("SELECT * FROM url_results WHERE page_type IS NOT NULL LIMIT 5", conn)
print(df2)
