"""Write operations shared by the API and tests. Every change by a person is recorded in audit_log."""
from __future__ import annotations

import secrets
import urllib.parse
from typing import Any, Dict, Optional

from qc6.domain import fixes as fx
from qc6.workers.journey import parse as parse_journey


class NotAllowed(ValueError):
    pass


async def create_workspace(pool, name: str, owner_email: str, owner_name: str) -> Dict[str, int]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            ws = await conn.fetchval("INSERT INTO workspaces (name) VALUES ($1) RETURNING id", name)
            mid = await conn.fetchval("INSERT INTO members (workspace_id, email, name, role) VALUES ($1,$2,$3,'owner') RETURNING id",
                                      ws, owner_email.lower(), owner_name)
    return {"workspace_id": ws, "member_id": mid}


async def add_member(pool, workspace_id: int, email: str, name: str, role: str = "editor") -> int:
    return await pool.fetchval("INSERT INTO members (workspace_id, email, name, role) VALUES ($1,$2,$3,$4) "
                               "ON CONFLICT (workspace_id, email) DO UPDATE SET name = EXCLUDED.name RETURNING id",
                               workspace_id, email.lower(), name, role)


async def create_site(pool, workspace_id: int, name: str, base_url: str, settings: Optional[Dict[str, Any]] = None,
                      actor_id: Optional[int] = None) -> int:
    u = urllib.parse.urlsplit(base_url if "://" in base_url else "https://" + base_url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise NotAllowed("Enter the site's address, for example https://www.example.com")
    base = f"{u.scheme}://{u.netloc}"
    host = (u.hostname or "").lower() + (f":{u.port}" if u.port else "")
    defaults = {"rate_rps": 12, "visitors": 6, "mobile_share": 0.6, "consent": "accept", "locale": "en-IN",
                "timezone": "Asia/Kolkata"}
    sid = await pool.fetchval("INSERT INTO sites (workspace_id, name, base_url, primary_host, settings, access_token) "
                              "VALUES ($1,$2,$3,$4,$5,$6) RETURNING id", workspace_id, name, base, u.hostname.lower(),
                              {**defaults, **(settings or {})}, secrets.token_urlsafe(24))
    await log(pool, workspace_id, actor_id, "site.create", f"site:{sid}", {"base_url": base})
    return sid


async def save_journey(pool, site_id: int, source_yaml: str) -> int:
    spec = parse_journey(source_yaml)
    return await pool.fetchval("INSERT INTO journeys (site_id, name, spec, source_yaml) VALUES ($1,$2,$3,$4) "
                               "ON CONFLICT (site_id, name) DO UPDATE SET spec = EXCLUDED.spec, source_yaml = EXCLUDED.source_yaml "
                               "RETURNING id", site_id, spec["name"], spec, source_yaml)


async def start_run(pool, site_id: int, kind: str, options: Optional[Dict[str, Any]] = None,
                    actor_id: Optional[int] = None) -> int:
    if kind not in ("coverage", "visit", "journey", "full"):
        raise NotAllowed("kind must be coverage, visit, journey or full")
    busy = await pool.fetchrow("SELECT id, status FROM runs WHERE site_id = $1 AND status IN ('queued','running','pausing','paused')",
                               site_id)
    if busy:
        if busy["status"] == "paused":
            raise NotAllowed(f"Check {busy['id']} of this site is paused. Resume it or cancel it first.")
        raise NotAllowed(f"A check of this site is already {busy['status']} (run {busy['id']}). "
                         "Only one check per site runs at a time, to stay within the site's speed limit.")
    rid = await pool.fetchval("INSERT INTO runs (site_id, kind, options, created_by) VALUES ($1,$2,$3,$4) RETURNING id",
                              site_id, kind, options or {}, actor_id)
    ws = await pool.fetchval("SELECT workspace_id FROM sites WHERE id = $1", site_id)
    await log(pool, ws, actor_id, "run.start", f"run:{rid}", {"kind": kind})
    return rid


async def update_fix(pool, fix_id: int, actor_id: Optional[int], status: Optional[str] = None,
                     owner_id: Optional[int] = None, unassign: bool = False, comment: Optional[str] = None,
                     ticket_url: Optional[str] = None) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            f = await conn.fetchrow("SELECT f.*, s.workspace_id FROM fixes f JOIN sites s ON s.id = f.site_id "
                                    "WHERE f.id = $1 FOR UPDATE", fix_id)
            if f is None:
                raise KeyError(fix_id)
            if status and status != f["status"]:
                if not fx.can_move(f["status"], status):
                    raise NotAllowed("Only a completed check can mark a fix verified." if status == "verified" else
                                     f"A fix can't move from {f['status'].replace('_', ' ')} to {status.replace('_', ' ')}.")
                await conn.execute("UPDATE fixes SET status = $2, regressed = false, updated_at = now() WHERE id = $1", fix_id, status)
                await conn.execute("INSERT INTO fix_events (fix_id, kind, actor_id, detail) VALUES ($1,'status',$2,$3)",
                                   fix_id, actor_id, {"from": f["status"], "to": status})
            if owner_id is not None or unassign:
                if owner_id is not None:
                    ok = await conn.fetchval("SELECT 1 FROM members WHERE id = $1 AND workspace_id = $2", owner_id, f["workspace_id"])
                    if not ok:
                        raise NotAllowed("That person isn't a member of this workspace.")
                await conn.execute("UPDATE fixes SET owner_id = $2, updated_at = now() WHERE id = $1", fix_id, owner_id)
                await conn.execute("INSERT INTO fix_events (fix_id, kind, actor_id, detail) VALUES ($1,'assigned',$2,$3)",
                                   fix_id, actor_id, {"owner_id": owner_id})
            if comment:
                await conn.execute("INSERT INTO fix_events (fix_id, kind, actor_id, detail) VALUES ($1,'comment',$2,$3)",
                                   fix_id, actor_id, {"text": comment[:4000]})
            if ticket_url:
                await conn.execute("UPDATE fixes SET ticket_url = $2 WHERE id = $1", fix_id, ticket_url[:500])
                await conn.execute("INSERT INTO fix_events (fix_id, kind, actor_id, detail) VALUES ($1,'ticket',$2,$3)",
                                   fix_id, actor_id, {"url": ticket_url[:500]})
            await conn.execute("INSERT INTO audit_log (workspace_id, actor_id, action, target, detail) VALUES ($1,$2,'fix.update',$3,$4)",
                               f["workspace_id"], actor_id, f"fix:{fix_id}",
                               {k: v for k, v in {"status": status, "owner_id": owner_id, "comment": bool(comment)}.items() if v})
            return dict(await conn.fetchrow("SELECT * FROM fixes WHERE id = $1", fix_id))


async def log(pool, workspace_id, actor_id, action: str, target: str, detail: Dict[str, Any]) -> None:
    await pool.execute("INSERT INTO audit_log (workspace_id, actor_id, action, target, detail) VALUES ($1,$2,$3,$4,$5)",
                       workspace_id, actor_id, action, target, detail)
