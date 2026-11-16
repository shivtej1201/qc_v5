# User Guide: Link & Image Auditor Platform

Welcome to the Next-Generation SEO Link and Image Auditor! This platform processes large URL files using background queues and bounded-memory ingestion. Jobs can contain up to 1,000,000 URL records by default.

## System Requirements
* Python 3.9 or higher
* Playwright browsers installed (`playwright install chromium`)

---

## 1. Starting the Application
**On Windows:**
Simply double-click the `start.bat` file in the project folder.

**On macOS / Linux:**
Run the shell script:
```bash
./start.sh
```
This will automatically launch both the Backend Web Server and the Background Processing Daemon. Open your browser and navigate to:
`http://localhost:8002`

---

## 2. Modes of Operation
1. **URL Status Check:** Supply a list of destination URLs. The tool checks their HTTP status and network health.
2. **Link Audit:** Supply a list of pages. The tool crawls the "Main Content" (ignoring footers), extracts <a> tags, checks the links, and provides a report showing where the broken links live.
3. **Image Audit:** Supply a list of pages. The tool extracts images, fetches metadata, verifies DAM platforms, checks the image URL, and generates an Image Audit report.

---

## 3. Uploading & Processing
1. Go to `http://localhost:8002`.
2. Select your Mode (e.g., Image Audit).
3. Select your `.xlsx` Excel file. The platform supports **Multi-Sheet** Excel files.
4. The system will inspect your workbook and display all the sheets containing a `URL` column.
5. **Force Recheck:** Check the *Force Recheck (Ignore Cache)* box if you want to bypass previously saved checks and re-verify everything live.
6. Click **Start Processing Jobs**.

**Large files:** CSV is recommended for very large imports because it is streamed in bounded chunks. XLSX is read in streaming mode, but each worksheet is limited by Excel to 1,048,576 total rows, including its header. The default per-job input limit is 1,000,000 URL records and can be changed with `MAX_URLS_PER_JOB` and `LINK_AUDIT_MAX_URL_RECORDS` in the environment. URL Status report generation is also streamed. Link and Image Audit jobs may discover many more relationships than input pages, and their report memory use depends on the number of discovered links/images; use URL Status mode when the goal is checking a very large flat URL list. Processing time still depends on network response times, target-host rate limits, and configured concurrency.

**Throughput settings:** Defaults are tuned for higher parallelism: 8 background workers, up to 200 concurrent URL Status checks (20 per host), and 32 Link Audit destination checks (10 per host). Override with `WORKER_COUNT`, `HTTP_CONCURRENCY`, `PER_DOMAIN_CONCURRENCY`, `LINK_AUDIT_CONCURRENCY`, and `LINK_AUDIT_PER_HOST_CONCURRENCY` in `.env`, then restart the application. Start with these defaults and reduce per-host concurrency if remote sites return `429` or `503`; reaching 10,000 checks in 10 minutes depends on response latency, unique destinations, cache hits, and remote rate limits.

---

## 4. The Job Monitor Dashboard
Once started, your jobs enter a background queue. You do not need to keep the browser tab open; the background daemon runs independently!

* **Live Polling:** The dashboard will display the live percentage of processing.
* **Pause / Resume:** If you need to stop work temporarily, click **Pause**. Click **Resume** when ready. No data is lost.
* **View Stats:** Click this next to a Job to see a real-time modal breakdown:
  * Total Extracted vs Unique URLs
  * Working vs Broken vs Redirects
  * Strict Network Failures (DNS, Timeouts, Connection Errors)
  * Pending Validations

---

## 5. Performance Benchmarking
We have included a dedicated benchmarking script that calculates exact throughput based on your SQLite data. To measure your actual extraction and validation URLs-per-second:
```bash
python benchmark.py
```
This reads completed jobs and outputs exact runtime, total extraction coverage, and Validation Throughput.

## 6. Downloading Final Reports
The platform enforces a **Strict Validation Gate**.
* You cannot download a "Final" report while a job is still `QUEUED` or `RUNNING`.
* The final Excel file guarantees 100% of discovered unique URLs have been fully checked.
* The output file maintains your exact required column structure.
* **Timestamps:** Look at the extreme right columns. You will see `extracted_timestamp` and `checked_timestamp` in ISO 8601 UTC format.

If you Cancel a job early and download it, the file will be prefixed as `PARTIAL_audit_report.xlsx` so you know it did not pass the validation gate.

---

## 6. Understanding the Excel Columns
Depending on the mode chosen, the columns will exactly match the requested enterprise structure. 

**Image Audit Specific Columns:**
* `image_name`: Extracts the best alt text, or falls back to filename.
* `source_platform`: Identifies `Commerce`, `AEM dam`, or `dynamic media`.
* `is_dam_asset`: True/False based on URL patterns.

**Error Taxonomy:**
Instead of generic "Failed" messages, check the `remarks` column for precise networking truths:
* `DNS Error`
* `Timeout`
* `Connection Error`
* `SSL Error`
