"""Parquet + DuckDB facts store: export in bounded batches, query, and safe CSV."""
import csv
import os
import sqlite3

from qc6 import facts
from qc6.settings import settings


def _db(path):
    c = sqlite3.connect(path)
    c.execute("create table on_page_link_relationships (id integer primary key, job_id, source_page_url, source_page_status, "
              "source_page_final_url, found_url, normalized_found_url, found_url_type, url_syntax_type, found_url_scope, anchor_text, "
              "anchor_texts, link_types, rel, hreflang, occurrence_count, representative_location)")
    c.execute("create table on_page_validation_cache (job_id, normalized_url, url_scope, validation_state, initial_http_status, "
              "http_status, final_url, redirect_count, redirect_chain, response_time_ms, content_type, status_bucket)")
    c.execute("create table on_page_image_relationships (id integer primary key, job_id, source_page_url, source_page_status, "
              "source_page_final_url, image_url, normalized_image_url, image_type, internal_external, attribute_source, alt_present, "
              "alt_text, width, height, loading, occurrence_count)")
    c.execute("create table on_page_image_validation_cache (job_id, normalized_url, http_status, status_bucket, content_type, "
              "content_length, image_issue)")
    for i in range(250):
        c.execute("insert into on_page_link_relationships (job_id, source_page_url, found_url, normalized_found_url, found_url_type, "
                  "found_url_scope, anchor_text, occurrence_count) values ('J',?,?,?,'ANCHOR',?,?,1)",
                  (f"https://x.test/p{i % 10}", f"https://x.test/t{i % 30}", f"https://x.test/t{i % 30}",
                   "EXTERNAL" if i % 30 == 29 else "INTERNAL", '=HYPERLINK("evil")' if i == 0 else f"link {i}"))
    for t in range(30):
        c.execute("insert into on_page_validation_cache values ('J',?,'INTERNAL','DONE',?,?,?,?,'',12,'text/html',?)",
                  (f"https://x.test/t{t}", 301 if t == 5 else (404 if t == 3 else 200), 404 if t == 3 else 200,
                   "https://x.test/new" if t == 5 else "", 1 if t == 5 else 0, "NOT_FOUND" if t == 3 else "OK"))
    for i in range(40):
        c.execute("insert into on_page_image_relationships (job_id, source_page_url, image_url, normalized_image_url, image_type, "
                  "internal_external, alt_present, alt_text, occurrence_count) values ('J',?,?,?,'IMG','INTERNAL',?,?,1)",
                  (f"https://x.test/p{i % 10}", f"https://x.test/i{i % 8}.png", f"https://x.test/i{i % 8}.png", i % 4 != 0, "a"))
    c.commit()
    c.close()


def test_export_query_and_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(facts, "BATCH", 64)                    # several batches -> bounded memory path exercised
    db = str(tmp_path / "c.db")
    _db(db)
    assert facts.export(db, "J", 7) == {"links": 250, "images": 40}
    r = facts.query(7, "links", "broken")
    assert r["total"] == 9 and all(x["http_status"] == 404 for x in r["rows"])
    assert r["summary"] == {"rows": 250, "unique_urls": 30, "broken": 9, "redirects": 9}
    assert facts.query(7, "links", "redirect")["rows"][0]["final_url"] == "https://x.test/new"
    assert facts.query(7, "links", "external")["total"] == 8
    page = facts.query(7, "links", page_url="https://x.test/p3", sort="url", desc=True, limit=5)
    assert page["total"] == 25 and len(page["rows"]) == 5 and page["rows"][0]["url"] >= page["rows"][-1]["url"]
    assert facts.query(7, "images", "missing_alt")["total"] == 10
    try:
        facts.query(7, "links", "missing_alt")
        raise AssertionError("missing_alt is images only")
    except ValueError:
        pass
    out = str(tmp_path / "links.csv")
    assert facts.export_csv(7, "links", out) == 250
    rows = list(csv.DictReader(open(out, encoding="utf-8")))
    evil = [r for r in rows if "HYPERLINK" in r["anchor_text"]][0]
    assert evil["anchor_text"].startswith("'=")               # formula neutralised
    assert facts.export_csv(7, "links", out, "broken") == 9
