import asyncio
import os
import unittest
import httpx
from qcv5.api.server import app, db
from qcv5.core.config import settings
import uuid
import pandas as pd

class TestPhase112AuthAndOperations(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not db.conn:
            await db.connect()
        # Configure auth settings for testing
        settings.AUTH_REQUIRED = True
        settings.PROXY_SECRET_VALUE = "test-secret-phase11-2"
        settings.PROXY_SECRET_HEADER = "X-Proxy-Secret"
        settings.USER_HEADER = "X-Remote-User"
        settings.MAX_URLS_PER_JOB = 100
        
        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://test")
        
        self.valid_headers = {
            "X-Proxy-Secret": "test-secret-phase11-2",
            "X-Remote-User": "auditor_ops@example.com"
        }

    async def asyncTearDown(self):
        # Reset auth settings
        settings.AUTH_REQUIRED = False
        settings.PROXY_SECRET_VALUE = ""
        settings.MAX_URLS_PER_JOB = 10000
        await self.client.aclose()

    async def test_01_health_endpoints_exempt_from_auth(self):
        """Health endpoints must be accessible without any reverse proxy headers."""
        resp1 = await self.client.get("/api/health")
        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()
        self.assertIn("status", data1)
        self.assertIn("database", data1)
        self.assertIn("daemon", data1)

        resp2 = await self.client.get("/health")
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertEqual(data2["version"], "2.0.0")

    async def test_02_unauthenticated_request_rejected(self):
        """Unauthenticated requests to API endpoints must be rejected."""
        resp = await self.client.get("/api/jobs")
        # Should be 403 Forbidden or 401 Unauthorized
        self.assertIn(resp.status_code, [401, 403])

    async def test_03_invalid_proxy_secret_rejected(self):
        """Requests with missing or invalid proxy secret must receive 403."""
        headers = {
            "X-Proxy-Secret": "wrong-secret",
            "X-Remote-User": "auditor@example.com"
        }
        resp = await self.client.get("/api/jobs", headers=headers)
        self.assertEqual(resp.status_code, 403)
        self.assertIn("secret", resp.json()["detail"].lower())

    async def test_04_missing_user_identity_rejected(self):
        """Requests with valid secret but missing user header must receive 401."""
        headers = {
            "X-Proxy-Secret": "test-secret-phase11-2"
        }
        resp = await self.client.get("/api/jobs", headers=headers)
        self.assertEqual(resp.status_code, 401)
        self.assertIn("user", resp.json()["detail"].lower())

    async def test_05_authenticated_request_succeeds(self):
        """Requests with valid secret and remote user header succeed."""
        resp = await self.client.get("/api/jobs", headers=self.valid_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("jobs", resp.json())

    async def test_06_operational_limit_max_urls_enforced(self):
        """Uploading file with URLs exceeding MAX_URLS_PER_JOB must be rejected with 400."""
        # Create CSV with 150 URLs (limit is 100)
        csv_file = "test_oversized.csv"
        urls = [f"https://example.com/page/{i}" for i in range(150)]
        df = pd.DataFrame({"page_url": urls})
        df.to_csv(csv_file, index=False)

        try:
            with open(csv_file, "rb") as f:
                resp = await self.client.post(
                    "/api/upload",
                    files={"file": ("test_oversized.csv", f, "text/csv")},
                    data={"mode": "api_response_audit"},
                    headers=self.valid_headers
                )
            self.assertEqual(resp.status_code, 400)
            self.assertIn("exceeds the limit", resp.json()["detail"])
        finally:
            if os.path.exists(csv_file):
                os.remove(csv_file)

    async def test_07_cleanup_stale_artifacts(self):
        """Test artifact retention cleanup endpoint."""
        resp = await self.client.post(
            "/api/admin/cleanup-artifacts",
            params={"days": 30},
            headers=self.valid_headers
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("deleted_screenshots", data)

if __name__ == "__main__":
    unittest.main()
