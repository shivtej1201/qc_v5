import sqlite3
import pandas as pd
conn = sqlite3.connect('audit_cache.db')
job_id = 'a2d76117-0c66-44c9-97ac-9b3d5a94f3ed'
df = pd.read_sql(f"SELECT COUNT(*) FROM extracted_links WHERE job_id = '{job_id}'", conn)
print('Extracted:', df.iloc[0,0])
df2 = pd.read_sql(f"SELECT COUNT(*) FROM url_results WHERE job_id = '{job_id}'", conn)
print('Results:', df2.iloc[0,0])
df3 = pd.read_sql(f"SELECT url, page_type, business, classification_method FROM url_results WHERE job_id = '{job_id}' LIMIT 5", conn)
print(df3)
