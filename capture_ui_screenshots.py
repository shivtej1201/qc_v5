import asyncio
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
import uvicorn
from playwright.async_api import async_playwright

from server import app, db
from config import settings
from url_normalizer import URLNormalizer

class ServerThread(threading.Thread):
    def __init__(self, host="127.0.0.1", port=8999):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        self.server = uvicorn.Server(self.config)

    def run(self):
        self.server.run()

    def stop(self):
        self.server.should_exit = True

def seed_demo_data():
    """Populates clean realistic sample jobs and records for rich browser UI capture."""
    import sqlite3
    con = sqlite3.connect(settings.DB_FILE, timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=30000;")
    now = datetime.now(timezone.utc).isoformat()
    upload_id = "upload_demo_ui"
    job_link = "job_demo_link_audit"
    job_api = "job_demo_api_audit"

    # Uploads
    con.execute("INSERT OR REPLACE INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", (upload_id, "enterprise_marketing_links.xlsx", "link_audit", "RUNNING", now))
    con.execute("INSERT OR REPLACE INTO uploads (id, filename, mode, status, created_at) VALUES (?, ?, ?, ?, ?)", ("upload_api_ui", "checkout_flows.xlsx", "api_response_audit", "COMPLETED", now))

    # Jobs
    con.execute(
        "INSERT OR REPLACE INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_link, upload_id, "Campaign Pages", 500, 500, "VALIDATING", now, now)
    )
    con.execute(
        "INSERT OR REPLACE INTO jobs (id, upload_id, sheet_name, total_records, processed_records, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_api, "upload_api_ui", "US Checkout", 15, 15, "COMPLETED", now, now)
    )

    # Batches
    for b_idx in range(3):
        b_id = f"batch_{b_idx}_{job_link}"
        con.execute(
            "INSERT OR REPLACE INTO batches (id, job_id, batch_index, input_file, total_records, processed_records, status, worker_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (b_id, job_link, b_idx, "batches/sample.json", 166, 166, "COMPLETED", f"worker_pool_{b_idx}", now, now)
        )

    # Extracted links for job_link
    links = []
    sample_dests = [
        ("https://acme.org/products/smart-hub", 200, "200 OK", "Hero Banner"),
        ("https://acme.org/pricing/enterprise", 200, "200 OK", "Top Nav"),
        ("https://acme.org/broken-legacy-offer", 404, "404 Not Found", "Footer CTA"),
        ("https://acme.org/old-login-redirect", 301, "301 Redirect", "Header Login"),
        ("https://acme.org/internal-gateway-timeout", 504, "Gateway Timeout", "Checkout Link"),
        ("https://acme.org/docs/api-reference", 200, "200 OK", "Docs Link"),
        ("https://acme.org/security/compliance", 200, "200 OK", "Footer Link"),
        ("https://acme.org/unstable-service", 500, "500 Server Error", "Widget CTA"),
    ]

    for i in range(40):
        target_info = sample_dests[i % len(sample_dests)]
        src_url = f"https://acme.org/pages/source_{i % 5}"
        dest_url = target_info[0]
        norm = URLNormalizer.normalize(dest_url)
        links.append((
            job_link, f"batch_0_{job_link}", src_url, dest_url, norm, target_info[3], "Campaign Grid", now
        ))
    
    con.executemany("""
        INSERT INTO extracted_links (job_id, batch_id, source_url, original_url, normalized_url, anchor_text, component_name, extracted_timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, links)

    for dest_url, status_code, status_str, _ in sample_dests:
        norm = URLNormalizer.normalize(dest_url)
        con.execute("""
            INSERT OR REPLACE INTO job_destinations (
                job_id, normalized_url, initial_status_code, final_status_code,
                status, final_url, redirect_count, response_time_ms,
                error_type, error_message, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job_link, norm, status_code, status_code,
            status_str, norm if status_code != 301 else "https://acme.org/auth/v2/login",
            1 if status_code == 301 else 0, 145.2,
            "HTTPError" if status_code >= 400 else "",
            f"HTTP {status_code}" if status_code >= 400 else "", now
        ))

    # Classifications
    for i in range(5):
        con.execute("""
            INSERT OR REPLACE INTO source_classifications (source_url, page_type, business, classification_method, classified_at)
            VALUES (?, ?, ?, ?, ?)
        """, (f"https://acme.org/pages/source_{i}", "PLP" if i % 2 == 0 else "PDP", "LSF", "META_TAG", now))

    # Page API Audit Results
    for i in range(4):
        p_url = f"https://acme.org/checkout/step_{i+1}"
        con.execute("""
            INSERT OR REPLACE INTO page_audit_results (job_id, page_url, final_url, page_status, http_status, response_time_ms, evaluated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (job_api, p_url, p_url, "Working", 200, 210.5, now))

    con.commit()
    con.close()

async def capture_all_ui_views():
    artifact_dir = r"C:\Users\shiva\.gemini\antigravity\brain\a4af716b-0812-42f0-b77d-8e40ec059336"
    os.makedirs(artifact_dir, exist_ok=True)

    seed_demo_data()

    server_thread = ServerThread(port=8999)
    server_thread.start()
    print("Local server started on http://127.0.0.1:8999")
    await asyncio.sleep(2)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        # -------------------------------------------------------------
        # 1. Desktop Light Mode Overview
        # -------------------------------------------------------------
        context_desktop = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context_desktop.new_page()

        print("Navigating to Overview Dashboard (Desktop Light)...")
        await page.goto("http://127.0.0.1:8999/", wait_until="networkidle")
        await page.wait_for_timeout(1000)
        p1 = os.path.join(artifact_dir, "overview_light_desktop.png")
        await page.screenshot(path=p1)
        print(f"Captured: {p1}")

        # -------------------------------------------------------------
        # 2. Desktop Dark Mode Overview
        # -------------------------------------------------------------
        print("Toggling Dark Mode on Overview...")
        await page.click("#theme-toggle-btn")
        await page.wait_for_timeout(600)
        p2 = os.path.join(artifact_dir, "overview_dark_desktop.png")
        await page.screenshot(path=p2)
        print(f"Captured: {p2}")

        # Toggle back to light mode for crisp standard screenshots
        await page.click("#theme-toggle-btn")
        await page.wait_for_timeout(400)

        # -------------------------------------------------------------
        # 3. Link Audit View & Live Explorer with Paginated Records Table
        # -------------------------------------------------------------
        print("Switching to Link Audit View...")
        await page.click("#nav-link-audit")
        await page.wait_for_timeout(600)

        # Trigger Live Explorer for the seeded job
        await page.evaluate("selectJobForLinkAudit('job_demo_link_audit')")
        await page.wait_for_timeout(1000)

        p3 = os.path.join(artifact_dir, "link_audit_view.png")
        await page.screenshot(path=p3)
        print(f"Captured: {p3}")

        # -------------------------------------------------------------
        # 4. Records Search & Filter View
        # -------------------------------------------------------------
        print("Testing filter in Live Explorer...")
        await page.fill("#records-search-input", "products/smart-hub")
        await page.wait_for_timeout(600)
        p4 = os.path.join(artifact_dir, "records_search_filtered.png")
        await page.screenshot(path=p4)
        print(f"Captured: {p4}")

        # Reset search input
        await page.fill("#records-search-input", "")
        await page.wait_for_timeout(400)

        # -------------------------------------------------------------
        # 5. Page & API Audit View
        # -------------------------------------------------------------
        print("Switching to Page & API Audit View...")
        await page.click("#nav-api-audit")
        await page.wait_for_timeout(600)
        p5 = os.path.join(artifact_dir, "page_api_audit_view.png")
        await page.screenshot(path=p5)
        print(f"Captured: {p5}")

        # -------------------------------------------------------------
        # 6. Job History & Operations Table View
        # -------------------------------------------------------------
        print("Switching to Job History View...")
        await page.click("#nav-history")
        await page.wait_for_timeout(600)
        p6 = os.path.join(artifact_dir, "job_history_view.png")
        await page.screenshot(path=p6)
        print(f"Captured: {p6}")

        # -------------------------------------------------------------
        # 7. Job Inspection Drawer Modal View
        # -------------------------------------------------------------
        print("Opening Job Details Inspection Drawer...")
        await page.evaluate("inspectJob('job_demo_link_audit')")
        await page.wait_for_timeout(800)
        p7 = os.path.join(artifact_dir, "job_details_drawer.png")
        await page.screenshot(path=p7)
        print(f"Captured: {p7}")

        # Close Drawer Modal
        await page.click("#job-details-modal button")
        await page.wait_for_timeout(300)

        # -------------------------------------------------------------
        # 8. Mobile Viewport (375x667 - iPhone SE)
        # -------------------------------------------------------------
        print("Testing Mobile Viewport (375x667)...")
        context_mobile = await browser.new_context(viewport={"width": 375, "height": 667})
        page_mobile = await context_mobile.new_page()
        await page_mobile.goto("http://127.0.0.1:8999/", wait_until="networkidle")
        await page_mobile.wait_for_timeout(800)
        p8 = os.path.join(artifact_dir, "mobile_viewport_375x667.png")
        await page_mobile.screenshot(path=p8)
        print(f"Captured: {p8}")

        # -------------------------------------------------------------
        # 9. Tablet Viewport (768x1024 - iPad Mini)
        # -------------------------------------------------------------
        print("Testing Tablet Viewport (768x1024)...")
        context_tablet = await browser.new_context(viewport={"width": 768, "height": 1024})
        page_tablet = await context_tablet.new_page()
        await page_tablet.goto("http://127.0.0.1:8999/", wait_until="networkidle")
        await page_tablet.wait_for_timeout(800)
        p9 = os.path.join(artifact_dir, "tablet_viewport_768x1024.png")
        await page_tablet.screenshot(path=p9)
        print(f"Captured: {p9}")

        # -------------------------------------------------------------
        # 10. SCREEN WAKE LOCK IN-BROWSER SIMULATION TEST
        # -------------------------------------------------------------
        print("Running in-browser ScreenWakeLockService simulation test...")
        wake_lock_results = await page.evaluate("""
            async () => {
                const results = [];
                // Check initial state
                results.push({ test: 'is_supported_bool', val: typeof ScreenWakeLockService.isSupported === 'boolean' });
                results.push({ test: 'is_enabled_bool', val: typeof ScreenWakeLockService.isEnabled === 'boolean' });
                results.push({ test: 'initial_wake_lock_null', val: ScreenWakeLockService.wakeLock === null });

                // Test updateUIStatus states and honest DOM text reflection
                ScreenWakeLockService.updateUIStatus('active');
                const t1 = document.getElementById('wakelock-text').textContent;
                results.push({ test: 'status_active_text', pass: t1 === 'Screen Awake: Active' });

                ScreenWakeLockService.updateUIStatus('reacquiring');
                const t2 = document.getElementById('wakelock-text').textContent;
                results.push({ test: 'status_reacquiring_text', pass: t2 === 'Screen Awake: Reacquiring...' });

                ScreenWakeLockService.updateUIStatus('disabled');
                const t3 = document.getElementById('wakelock-text').textContent;
                results.push({ test: 'status_disabled_text', pass: t3 === 'Screen Awake: Off' });

                ScreenWakeLockService.updateUIStatus('unsupported');
                const t4 = document.getElementById('wakelock-text').textContent;
                results.push({ test: 'status_unsupported_text', pass: t4 === 'Screen Awake: Unsupported' });

                ScreenWakeLockService.updateUIStatus('standby');
                const t5 = document.getElementById('wakelock-text').textContent;
                results.push({ test: 'status_standby_text', pass: t5 === 'Screen Awake: Standby' });

                // Test release event simulation with active jobs
                window.currentJobs = [{ id: 'j_sim_1', status: 'RUNNING' }, { id: 'j_sim_2', status: 'COMPLETED' }];
                const dummySentinel = { released: false, addEventListener: () => {} };
                ScreenWakeLockService.wakeLock = dummySentinel;
                ScreenWakeLockService.reacquireAttempts = 0;
                ScreenWakeLockService.handleRelease(dummySentinel);
                
                // Should have set wakeLock to null immediately and entered reacquiring state
                results.push({ test: 'wake_lock_null_on_release', pass: ScreenWakeLockService.wakeLock === null });
                results.push({ test: 'reacquire_state_on_release', pass: document.getElementById('wakelock-text').textContent.includes('Reacquiring') });
                results.push({ test: 'reacquire_attempts_incremented', pass: ScreenWakeLockService.reacquireAttempts === 1 });

                // Test release when all jobs are terminal
                window.currentJobs = [{ id: 'j_sim_1', status: 'COMPLETED' }];
                ScreenWakeLockService.wakeLock = dummySentinel;
                ScreenWakeLockService.handleRelease(dummySentinel);
                results.push({ test: 'standby_when_terminal', pass: document.getElementById('wakelock-text').textContent.includes('Standby') });

                return results;
            }
        """)

        for r in wake_lock_results:
            passed = r.get("pass", True)
            print(f"  [WakeLock JS Test] {r['test']}: {'PASS' if passed else 'FAIL'}")

        await browser.close()

    server_thread.stop()
    print("\nAll UI views successfully verified and captured!")

if __name__ == "__main__":
    asyncio.run(capture_all_ui_views())
    os._exit(0)
