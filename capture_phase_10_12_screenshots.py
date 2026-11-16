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

class ServerThread(threading.Thread):
    def __init__(self, host="127.0.0.1", port=8998):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        self.server = uvicorn.Server(self.config)

    def run(self):
        self.server.run()

    def stop(self):
        self.server.should_exit = True

def seed_sitemap_demo_data():
    """Populates clean realistic sample sitemap import and diagnostics for UI capture."""
    import sqlite3
    con = sqlite3.connect(settings.DB_FILE, timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=30000;")
    now = datetime.now(timezone.utc).isoformat()
    import_id = "import_demo_ecommerce"

    # Sitemap Import
    con.execute(
        "INSERT OR REPLACE INTO sitemap_imports (id, title, status, total_sitemaps, processed_sitemaps, total_urls, unique_urls, duplicate_urls, rejected_urls, error_count, crawl_job_id, error_message, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (import_id, "Acme Store Global Sitemaps", "COMPLETED", 3, 3, 1420, 1380, 40, 0, 0, None, None, now, now)
    )

    # Discovered Sitemap Sources
    sources = [
        (import_id, "https://acme.store/sitemap.xml", "https://acme.store/sitemap.xml", 0, "PARSED", 200, 2, "INDEX", None, now),
        (import_id, "https://acme.store/sitemap-products.xml.gz", "https://acme.store/sitemap.xml", 1, "PARSED", 200, 1150, "URLSET", None, now),
        (import_id, "https://acme.store/sitemap-categories.xml", "https://acme.store/sitemap.xml", 1, "PARSED", 200, 270, "URLSET", None, now),
    ]
    for s in sources:
        con.execute(
            "INSERT OR REPLACE INTO sitemap_sources (import_id, sitemap_url, parent_sitemap_url, depth, status, http_status, urls_found, classification, error_message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            s
        )

    # Extracted URLs
    urls = [
        (import_id, "https://acme.store/sitemap-products.xml.gz", "https://acme.store/products/wireless-headphones", "https://acme.store/products/wireless-headphones", "VALID", 1, 1, None, "2026-10-10", "daily", 0.9, now),
        (import_id, "https://acme.store/sitemap-products.xml.gz", "https://acme.store/products/mechanical-keyboard", "https://acme.store/products/mechanical-keyboard", "VALID", 1, 1, None, "2026-10-09", "weekly", 0.8, now),
        (import_id, "https://acme.store/sitemap-products.xml.gz", "https://acme.store/products/ergonomic-mouse", "https://acme.store/products/ergonomic-mouse", "VALID", 1, 1, None, "2026-10-08", "weekly", 0.8, now),
        (import_id, "https://acme.store/sitemap-categories.xml", "https://acme.store/categories/audio", "https://acme.store/categories/audio", "VALID", 1, 1, None, "2026-10-01", "monthly", 0.7, now),
        (import_id, "https://acme.store/sitemap-categories.xml", "https://acme.store/categories/peripherals", "https://acme.store/categories/peripherals", "VALID", 1, 1, None, "2026-10-01", "monthly", 0.7, now),
        (import_id, "https://acme.store/sitemap-products.xml.gz", "https://acme.store/admin/staging-preview", "https://acme.store/admin/staging-preview", "ROBOTS_DISALLOWED", 0, 0, "Disallowed by robots.txt", "2026-09-15", "never", 0.1, now),
        (import_id, "https://acme.store/sitemap-categories.xml", "https://acme.store/checkout/cart-view", "https://acme.store/checkout/cart-view", "ROBOTS_DISALLOWED", 0, 0, "Disallowed by robots.txt", "2026-09-12", "never", 0.2, now),
        (import_id, "https://acme.store/sitemap-products.xml.gz", "https://acme.store/products/4k-monitor", "https://acme.store/products/4k-monitor", "VALID", 1, 1, None, "2026-10-11", "daily", 0.9, now)
    ]
    for u in urls:
        con.execute(
            "INSERT OR REPLACE INTO sitemap_extracted_urls (import_id, source_sitemap_url, raw_url, normalized_url, status, robots_allowed, selected_for_crawl, rejection_reason, lastmod, changefreq, priority, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            u
        )

    # 403 Diagnostics
    diags = [
        (import_id, None, "https://acme.store/admin/staging-preview", "https://acme.store/admin/staging-preview", "sitemap_download", 403,
         "CDN_WAF_BLOCKED_OR_CHALLENGED",
         "Cloudflare Managed Challenge / WAF intercepted request with HTTP 403 and challenge headers",
         "Initial sitemap XML retrieval handshake",
         "Response contained 'cf-mitigated: challenge' and CF-Ray header",
         "Configure crawler IP allowlist in Cloudflare WAF, or supply authenticated session bypass",
         0, '{"Server": "cloudflare", "CF-Ray": "893df102-SJC", "Content-Type": "text/html"}', None, now),
        (import_id, None, "https://acme.store/checkout/cart-view", "https://acme.store/checkout/cart-view", "robots_txt_fetch", None,
         "ROBOTS_BLOCKED",
         "Robots.txt directive explicitly disallows path '/checkout/' for User-agent: *",
         "Pre-crawl policy verification",
         "robots.txt rule Disallow: /checkout/ matched",
         "Verify site robots.txt policy if this section was intended for public search indexing",
         0, '{"Status": "Policy Enforced"}', None, now)
    ]
    for d in diags:
        con.execute(
            "INSERT OR REPLACE INTO request_diagnostics (import_id, job_id, url, final_url, stage, http_status, classification, what_happened, where_it_happened, what_verified, recommended_action, retry_appropriate, safe_headers, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            d
        )

    con.commit()
    con.close()
    print("Demo data seeded successfully.")

async def capture_sitemap_views():
    seed_sitemap_demo_data()

    server_thread = ServerThread(port=8998)
    server_thread.start()
    time.sleep(1.5)

    artifact_dir = r"C:\Users\shiva\.gemini\antigravity\brain\a4af716b-0812-42f0-b77d-8e40ec059336"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        print("Navigating to dashboard...")
        await page.goto("http://127.0.0.1:8998/", wait_until="networkidle")
        await page.wait_for_timeout(800)

        # 1. Click Sitemap to Crawl Navigation Button
        print("Navigating to Sitemap to Crawl view...")
        await page.click("#nav-sitemap-crawl")
        await page.wait_for_timeout(800)

        p1 = os.path.join(artifact_dir, "sitemap_crawl_input_view.png")
        await page.screenshot(path=p1)
        print(f"Captured: {p1}")

        # 2. Trigger view of the completed import in the UI
        print("Loading extracted URLs and sources in UI...")
        await page.evaluate("""
            async () => {
                currentSitemapImportId = 'import_demo_ecommerce';
                document.getElementById('sitemap-progress-card').classList.remove('hidden');
                document.getElementById('sitemap-urls-card').classList.remove('hidden');
                document.getElementById('sitemap-crawl-launch-card').classList.remove('hidden');
                await refreshSitemapStatus();
                await loadSitemapDiagnostics();
                document.getElementById('sitemap-urls-card').scrollIntoView();
            }
        """)
        await page.wait_for_timeout(1000)

        p2 = os.path.join(artifact_dir, "sitemap_extracted_urls_wizard.png")
        await page.screenshot(path=p2)
        print(f"Captured: {p2}")

        # 3. View 403 Diagnostics Cards
        print("Scrolling to 403 Diagnostics Breakdown...")
        await page.evaluate("""
            () => {
                document.getElementById('sitemap-diagnostics-section').scrollIntoView();
            }
        """)
        await page.wait_for_timeout(600)

        p3 = os.path.join(artifact_dir, "sitemap_403_diagnostics_cards.png")
        await page.screenshot(path=p3)
        print(f"Captured: {p3}")

        # 4. View Dark Mode of the Sitemap Extracted URLs Wizard
        print("Capturing Dark Mode Sitemap Extracted URLs view...")
        await page.evaluate("""
            () => {
                window.scrollTo(0, 0);
            }
        """)
        await page.wait_for_timeout(300)
        await page.click("#theme-toggle-btn")
        await page.wait_for_timeout(600)

        p4 = os.path.join(artifact_dir, "sitemap_extracted_urls_dark.png")
        await page.screenshot(path=p4)
        print(f"Captured: {p4}")

        await browser.close()

    server_thread.stop()
    print("\nAll Sitemap to Crawl UI views successfully captured!")

if __name__ == "__main__":
    asyncio.run(capture_sitemap_views())
    os._exit(0)
