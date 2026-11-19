# On-page audit API — frozen contract (v1.0)

Machine-readable schema: [`openapi_onpage.json`](openapi_onpage.json) (generated from the running FastAPI app, so it
cannot drift from the code; regenerate after any change and diff it in review). The frontend is built against this
contract only.

## Conventions

| Topic | Rule |
|---|---|
| Base path | `/api/on-page` (on-page audits), `/api/jobs/{job_id}/remediations*` (remediation, shared with classic audits) |
| Auth | Reverse-proxy authentication middleware (`AUTH_REQUIRED` / `PROXY_SECRET_VALUE`); `/api/health` is open. Single tenant: there is no workspace/RBAC model in this codebase |
| IDs | audit `onpage_[a-f0-9]{12}`, issue key `[a-f0-9]{20}`, sitemap import `smi_[a-f0-9]{12}`, profile `smp_[a-f0-9]{10}`; anything else → 400 |
| Paging | `offset` (≥ 0) + `limit` (1–500); response `{items, matched, offset, limit, sort, order}`; `matched` = total rows for the filter |
| Sorting | `sort` from a per-endpoint whitelist (else 400), `order=asc|desc`; ties broken by row id (stable pages) |
| Filtering | exact-match filters + `q` substring search; all values are bound parameters |
| Errors | `{"detail": "..."}`; 400 invalid input / id / sort / limit, 404 unknown audit, URL, issue or report not built, 409 domain already crawling / audit running / cancelled audit resumed, 422 body schema |
| Time | ISO-8601 UTC strings (`...Z`) |
| CSV | UTF-8, RFC 4180 quoting, formula-injection guard (cells starting `= + - @ \t \r` are prefixed with `'`, URLs excepted) |

## Audit lifecycle

| Method & path | Request | Response / notes |
|---|---|---|
| `GET /presets` | — | `{presets: {key: {label, description, target_rps, max_rps, source_concurrency, dest_concurrency, image_concurrency}}}` (`conservative`, `target`, `max_tested`, `enterprise_150k`) |
| `GET /estimate?pages=&rps=&images_on_primary=` | — | `{estimated_minutes, meets_20_min_target, rps_needed_for_20_min, ...}` from the measured request model |
| `POST /audits` | `{name, urls[] \| sitemap{import_id, selected_sitemaps[], include[], exclude[], max_pages}, preset, overrides{target_rps, max_rps, min_rps, source_concurrency, dest_concurrency, image_concurrency, per_host_concurrency, read_timeout, max_retries}, primary_domain}` | audit descriptor (below). Every non-empty input line is either a page or a row in `17_input_exceptions.csv` |
| `POST /audits/upload` | multipart `file` (.txt/.csv/.xlsx), `name`, `preset`, `target_rps`, `primary_domain` | audit descriptor |
| `GET /audits` | — | `{audits: [descriptor + stats]}` |
| `GET /audits/{id}` | — | descriptor + `stats` (DB counters + `live_metrics`) + `progress` + `health` + `score` + `log_tail` |
| `GET /audits/{id}/progress` | — | `{lifecycle, total, processed, remaining, queued, success, failed, blocked, skipped, percent, links, images, unique_urls, unique_images, issues, throughput_current_pps, throughput_average_pps, requests_per_sec_primary, elapsed_sec, eta_sec, source:"backend"}` |
| `POST /audits/{id}/pause` · `/stop` | — | graceful; in-flight work committed; resumable |
| `POST /audits/{id}/resume` | — | continues from the checkpoint (409 if running, cancelled, or the domain is busy) |
| `POST /audits/{id}/cancel` | — | stops for good; completed work stays queryable; not resumable; no package |

Audit descriptor: `{job_id, name, created_at, preset, config, url_count, primary_domain, source, run_state, lifecycle,
events[{state, at, note}], final_qc_status, crawl_seconds, package_available, estimate}`.

`lifecycle` ∈ `CREATED, QUEUED, RUNNING, PAUSING, PAUSED, RESUMED, CANCELLING, CANCELLED, COMPLETED, PARTIAL, FAILED,
INTERRUPTED` (INTERRUPTED = worker died; resume continues). `run_state` is kept for compatibility.

## Results (available once the report package is built)

| Method & path | Filters | Sort keys | Item fields |
|---|---|---|---|
| `GET /audits/{id}/urls` | `status_bucket=2xx\|3xx\|4xx\|5xx`, `crawl_state`, `q` | `id, status, url, time, links` | page id/url, crawl_state, initial/final status, final_url, redirects, response_time_ms, content_type, title, link + image counts, business, page_type, language, source_sitemap, indexable, meta_robots, h1_count, word_count, title/description lengths |
| `GET /audits/{id}/url-detail?url=` | — | — | `overview, http, redirect{count, chain, final_url}, canonical{url, status, final_url, self_referencing}, robots, metadata, headings, hreflang, structured_data, content, performance, security, links{by_type}, images{placements, missing_alt, broken}, issues[], raw{source_page, seo}` |
| `GET /audits/{id}/links` | `scope=INTERNAL\|EXTERNAL\|...`, `url_type` (ANCHOR, CANONICAL, HREFLANG, SCRIPT, STYLESHEET, FORM, JSONLD_URL, …), `status_bucket`, `source_page`, `q` | `id, status, url, page` | source page, found + normalized URL, url_type, internal_external, anchor_text, rel, component, language, business, page_type (from the SOURCE page), validation_source, http/initial status, final_url, redirect_count, status_bucket, error_type |
| `GET /audits/{id}/images` | `internal_external`, `status_bucket`, `alt=missing\|empty\|present`, `source_page`, `q` | `id, status, url, size` | source page, image + normalized URL, attribute_source, alt, width, height, loading, http status, content type/length, status_bucket, image_issue, error_type |
| `GET /audits/{id}/issues` | `severity`, `issue_type`, `category`, `status`, `page_url`, `q` | `severity, rule, page, status, id` | issue_key, severity, category, issue_type, dimension, page, element, found/normalized URL, anchor/alt, location, statuses, final_url, hops, detail, recommended_action, status, status_note |
| `GET /audits/{id}/issues/{issue_key}` | — | — | issue + `evidence{...}` + `recommended_action` + lifecycle (`status, status_note, status_updated_at, status_job_id`) |
| `PATCH /audits/{id}/issues/{issue_key}/status` | `{status: OPEN\|IN_PROGRESS\|VERIFIED\|RESOLVED\|DISMISSED, note}` | — | persisted per project; see lifecycle rules in the architecture doc |
| `GET /audits/{id}/issues/overview` | — | — | `{by_severity, types[], total, summary{01_summary}, top_high_urls[]}` |
| `GET /audits/{id}/score` | — | — | `{score, technical, links, content, performance, security, pages_scored, formula_version, weights, calculated_at}` |
| `GET /audits/{id}/seo` | — | — | SEO score, coverage per check, SEO issue areas, sitemap health rows |
| `GET /audits/{id}/insights` · `/pages` | — | — | smart insights · worst pages |
| `GET /audits/{id}/package` | — | — | ZIP (streamed `FileResponse`, never buffered in memory) |
| `GET /audits/{id}/files/{NN_name.csv}` | — | — | one CSV (name must match `^\d\d_[a-z_]+\.csv$`) |

## Sitemaps

| Method & path | Request | Response |
|---|---|---|
| `POST /sitemaps/discover` | `{inputs[1..20]: sitemap URL \| robots.txt URL \| domain, max_urls}` | import snapshot: `{import_id, inputs, discovery[], sources[{sitemap_url, parent_sitemap_url, depth, kind, status, http_status, classification, urls_listed, unique_urls, duplicates, rejected, size_bytes, content_type, response_time_ms, child_sitemap_count, robots_blocked, lastmod_missing/invalid/future, error_message}], summary, errors}` |
| `GET /sitemaps/{import_id}` | — | same snapshot (without the URL list) |
| `POST /sitemaps/{import_id}/preview` | `{import_id, selected_sitemaps[], include[], exclude[], max_pages, include_robots_blocked}` | `{selected_count, excluded{sitemap, robots, include, exclude, cap}, sample[], estimate{preset: ...}}` |
| `GET/POST /sitemap-profiles` · `PUT/DELETE /sitemap-profiles/{id}` · `POST /sitemap-profiles/{id}/run` | profile `{name, inputs[], selected_sitemaps[], include[], exclude[], max_pages, preset, overrides, primary_domain}` | profile; *run* → `{import, audit}` |

`classification` separates `BLOCKED_BY_TARGET_OR_WAF` / `SITEMAP_HTTP_403` / `SITEMAP_HTTP_404` / `SITEMAP_HTTP_5XX` /
`SITEMAP_TIMEOUT` / `ROBOTS_BLOCKED` from `SITEMAP_XML_INVALID` (parse errors).

## Remediation (existing module, now fed by on-page issues)

`POST /api/jobs/{id}/remediations/generate` → `{generated_count}`; `GET /api/jobs/{id}/remediations?page=&page_size=&severity=&priority=&category=&status=&q=&sort_by=&sort_dir=` →
`{records[], total_records, total_pages, ...}`; on-page items have `category = ONPAGE_<RULE>` and
`technical_details.scope = destination | page | site-wide`; `PATCH /api/jobs/{id}/remediations/{rid}` statuses
`OPEN, IN_PROGRESS, VERIFIED, RESOLVED, DISMISSED, NEEDS_REVIEW`.

## Report package (ZIP)

`00_manifest.csv` (filename, dataset, row_count, expected_rows, columns, file_size, sha256, generated_at, status) ·
`01_summary` · `02_source_page_audit` (URL audit) · `03_url_audit` (link audit, every URL category) ·
`04_unique_url_validation` · `05_image_audit` · `06_unique_image_validation` · `07_broken_urls` · `08_redirect_urls` ·
`09_broken_images` · `10_external_urls` · `11_reconciliation` · `12_issues` · `13_issue_summary` ·
`14_page_issue_summary` · `15_seo_page_audit` · `16_sitemap_audit` · `17_input_exceptions`. Columns are only ever
appended (existing column order is stable). The original 14 datasets (01–14) are unchanged in meaning.

## Change log vs. the previous API

* **Added**: `pause`, `cancel`, `progress`, `score`, `url-detail`, `issues/{key}`, `issues/{key}/status`, `urls`,
  `links`, `images`, sitemap discovery/preview/profiles, `seo`; issues filters `status`, `page_url`, `sort`, `order`.
* **Modified**: `GET /audits/{id}` adds `lifecycle`, `events`, `progress`, `score`; `GET /issues` reads the database
  (falls back to the CSV for audits packaged before issue persistence); remediation statuses add `VERIFIED`.
* **Deprecated**: `run_state` (use `lifecycle`); `POST /stop` (use `pause`).
* **Removed**: nothing.
