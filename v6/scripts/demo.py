"""Local demo: serve the fixture site on a fixed port and seed a workspace, a site and two journeys.
Usage (from v6/):  QC6_ALLOW_PRIVATE_TARGETS=true python scripts/demo.py --port 8892
Then start the API (uvicorn qc6.api.app:app --port 8003) and a worker (python -m qc6.workers.runner)."""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from http.server import ThreadingHTTPServer  # noqa: E402

from fixture_site import Handler  # noqa: E402
from qc6 import services  # noqa: E402
from qc6.db.migrate import migrate  # noqa: E402
from qc6.db.pool import get_pool  # noqa: E402

PASS = """name: Check loan eligibility
device: mobile
steps:
  - open: /apply
    expect: [ { visible: "role=heading[name=/eligibility/i]" } ]
  - fill: { "label=Monthly income": "85000" }
  - select: { "label=City": "Pune" }
  - click: "role=button[name='Continue']"
    expect: [ { visible: "text=/You are eligible/" }, { no_console_errors: true } ]
  - stop_before: "role=button[name='Submit application']"
"""
FAIL = PASS.replace("Check loan eligibility", "Check eligibility on desktop").replace("device: mobile", "device: desktop").replace("/apply\n", "/apply-broken\n")


async def seed(base: str):
    await migrate()
    pool = await get_pool()
    if await pool.fetchval("SELECT COUNT(*) FROM sites"):
        print("already seeded")
        return
    ws = await services.create_workspace(pool, "Demo Finance", "priya@demo.test", "Priya Nair")
    await services.add_member(pool, ws["workspace_id"], "rahul@demo.test", "Rahul Mehta", "editor")
    await services.add_member(pool, ws["workspace_id"], "ananya@demo.test", "Ananya Iyer", "editor")
    sid = await services.create_site(pool, ws["workspace_id"], "Demo store", base, {"rate_rps": 30, "visitors": 6})
    await services.save_journey(pool, sid, PASS)
    await services.save_journey(pool, sid, FAIL)
    print("seeded site", sid)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8892)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    asyncio.run(seed(f"http://127.0.0.1:{a.port}"))
    print(f"fixture site on http://127.0.0.1:{a.port}")
    srv.serve_forever()
