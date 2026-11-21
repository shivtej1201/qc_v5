"""Alerts to Slack and Microsoft Teams (free incoming webhooks), written for people.

Site settings:
    "alerts": {"slack_webhook": "https://hooks.slack.com/services/…",
               "teams_webhook": "https://<tenant>.webhook.office.com/…",
               "on": ["summary", "regressed", "journey_failed", "access"]}   # default: all four

Webhook URLs are only accepted on the vendors' own hosts (no requests to arbitrary or internal addresses) and are
never returned by the API in full.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional

import httpx

from qc6.domain.rules import format_count

ALLOWED_HOSTS = {"slack": ("hooks.slack.com",), "teams": (".webhook.office.com", "outlook.office.com", ".logic.azure.com")}
_transport: Optional[httpx.AsyncBaseTransport] = None          # tests swap in a MockTransport


def set_transport(t: Optional[httpx.AsyncBaseTransport]) -> None:
    global _transport
    _transport = t


def valid_webhook(channel: str, url: str) -> bool:
    u = urllib.parse.urlsplit(url or "")
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h.lstrip(".") or host.endswith(h) for h in ALLOWED_HOSTS.get(channel, ()))


def mask(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{u.hostname}/…{url[-4:]}"


async def _post(channel: str, url: str, text: str, lines: List[str]) -> int:
    body: Dict[str, Any]
    if channel == "slack":
        body = {"text": text, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{text}*"}}] +
                ([{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(f"• {l}" for l in lines[:8])}}] if lines else [])}
    else:
        body = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
            "body": [{"type": "TextBlock", "text": text, "weight": "Bolder", "wrap": True}] +
                    [{"type": "TextBlock", "text": f"• {l}", "wrap": True, "spacing": "None"} for l in lines[:8]]}}]}
    kw: Dict[str, Any] = {"timeout": 15}
    if _transport is not None:
        kw["transport"] = _transport
    async with httpx.AsyncClient(**kw) as c:
        r = await c.post(url, json=body)
        return r.status_code


async def messages(pool, run_id: int, site: Dict[str, Any], summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What to say about a finished check, in plain words."""
    out = []
    name = site["name"]
    regressed = await pool.fetch("SELECT title FROM fixes WHERE site_id = $1 AND regressed AND status = 'open' "
                                 "AND last_seen_run = $2 ORDER BY pages_affected DESC LIMIT 5", site["id"], run_id)
    if regressed:
        out.append({"kind": "regressed", "text": f"{name}: {len(regressed)} fixed problem{'s' if len(regressed) != 1 else ''} came back",
                    "lines": [r["title"] for r in regressed]})
    failed = await pool.fetch("""SELECT j.name, jr.failed_step, jr.failure_text FROM journey_runs jr JOIN journeys j ON j.id = jr.journey_id
                                 WHERE jr.run_id = $1 AND jr.status != 'passed'""", run_id)
    if failed:
        out.append({"kind": "journey_failed", "text": f"{name}: {len(failed)} journey{'s' if len(failed) != 1 else ''} failed",
                    "lines": [f"\"{f['name']}\" at step {f['failed_step']}: {(f['failure_text'] or '')[:180]}" for f in failed]})
    if summary.get("challenged"):
        out.append({"kind": "access", "text": f"{name}: the CDN stopped our visitors as bots",
                    "lines": ["Real-visitor checks stopped. Send the allow rule in Settings → Crawler access to your CDN team."]})
    parts = []
    if summary.get("pages"):
        parts.append(f"{format_count(int(summary['pages']))} pages checked")
    if summary.get("visited"):
        parts.append(f"{format_count(int(summary['visited']))} visited in a real browser")
    if summary.get("journeys"):
        parts.append(f"{summary.get('journeys_passed', 0)} of {summary['journeys']} journeys passed")
    lines = []
    if summary.get("score") is not None:
        lines.append(f"Health score {round(summary['score'])} out of 100")
    for t in summary.get("templates_changed", [])[:3]:
        lines.append(f"{t['template']} look different since the last check ({round(t['changed'] * 100)}% of the page changed)")
    lc = summary.get("lifecycle", {}) or {}
    if lc.get("verified"):
        lines.append(f"{lc['verified']} fix{'es' if lc['verified'] != 1 else ''} confirmed")
    out.append({"kind": "summary", "text": f"{name}: check finished" + (f" — {', '.join(parts)}" if parts else ""), "lines": lines})
    return out


async def after_run(pool, run_id: int, site: Dict[str, Any], summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    cfg = (site.get("settings") or {}).get("alerts") or {}
    wanted = set(cfg.get("on") or ["summary", "regressed", "journey_failed", "access"])
    channels = [(ch, cfg.get(f"{ch}_webhook")) for ch in ("slack", "teams")]
    channels = [(ch, url) for ch, url in channels if url and valid_webhook(ch, url)]
    if not channels:
        return []
    sent = []
    for m in await messages(pool, run_id, site, summary):
        if m["kind"] not in wanted:
            continue
        for ch, url in channels:
            status, err = None, None
            try:
                status = await _post(ch, url, m["text"], m["lines"])
            except httpx.HTTPError as e:
                err = f"{type(e).__name__}: {e}"[:300]
            await pool.execute("INSERT INTO alerts_sent (site_id, run_id, channel, kind, status, error) VALUES ($1,$2,$3,$4,$5,$6)",
                               site["id"], run_id, ch, m["kind"], status, err)
            sent.append({"channel": ch, "kind": m["kind"], "status": status, "error": err})
    return sent
