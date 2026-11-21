"""Extra free checks on one page per page type (blueprint §3, phase 5). Each runs only when configured:

    QC6_NU_URL             self-hosted W3C Nu Html Checker, e.g. http://nu:8888   (docker: ghcr.io/validator/validator)
    QC6_LANGUAGETOOL_URL   self-hosted LanguageTool server, e.g. http://lt:8010   (docker: erikvl87/languagetool)
    QC6_LIGHTHOUSE_CMD     path to the Lighthouse CLI (npm i lighthouse), run locally against Chromium
    QC6_CHROME_PATH        Chromium for Lighthouse (defaults to Playwright's)

These services run inside your own network, so their URLs are trusted configuration (not user input). The pages
they look at are fetched by QC with its honest identity and the SSRF guard, like every other request.
"""
from __future__ import annotations

import asyncio
import html as htmllib
import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional

import httpx

from qc6.settings import settings
from qc6.workers.analysis import Item

_transport: Optional[httpx.AsyncBaseTransport] = None


def set_transport(t) -> None:
    global _transport
    _transport = t


def configured() -> Dict[str, bool]:
    return {"nu": bool(os.environ.get("QC6_NU_URL")), "languagetool": bool(os.environ.get("QC6_LANGUAGETOOL_URL")),
            "lighthouse": bool(os.environ.get("QC6_LIGHTHOUSE_CMD"))}


_TAGS = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.S | re.I)
_BLOCK = re.compile(r"<(title|h[1-6]|p|li|td|th|figcaption|blockquote|button|a)\b[^>]*>(.*?)</\1>", re.S | re.I)
_ANY = re.compile(r"<[^>]+>")


def visible_text(html: str, limit: int = 4000) -> str:
    """Headings, paragraphs, list items, buttons and links, in order: the words a visitor reads."""
    h = _TAGS.sub(" ", html or "")
    parts = []
    for _, inner in _BLOCK.findall(h):
        t = " ".join(htmllib.unescape(_ANY.sub(" ", inner)).split())
        if len(t) >= 3:
            parts.append(t if t.endswith((".", "!", "?", ":")) else t + ".")
        if sum(len(p) for p in parts) > limit:
            break
    return " ".join(parts)[:limit]


async def _client(timeout: float = 30) -> httpx.AsyncClient:
    kw: Dict[str, Any] = {"timeout": timeout}
    if _transport is not None:
        kw["transport"] = _transport
    return httpx.AsyncClient(**kw)


async def fetch_html(url: str, site: Dict[str, Any]) -> Optional[str]:
    from qcv5.crawler.url_normalizer import URLNormalizer
    if not settings.allow_private_targets and not URLNormalizer.is_safe_url(url):
        return None
    async with await _client() as c:
        r = await c.get(url, headers={"User-Agent": f"Mozilla/5.0 {settings.ua_suffix}", settings.access_header: site["access_token"]})
        return r.text if r.status_code == 200 and "html" in r.headers.get("content-type", "html") else None


async def nu_check(html: str) -> Dict[str, Any]:
    async with await _client() as c:
        r = await c.post(os.environ["QC6_NU_URL"].rstrip("/") + "/?out=json", content=html.encode("utf-8"),
                         headers={"Content-Type": "text/html; charset=utf-8"})
        r.raise_for_status()
        msgs = r.json().get("messages", [])
    errors = [m for m in msgs if m.get("type") == "error"]
    return {"errors": len(errors), "first": [f"line {m.get('lastLine', '?')}: {m.get('message', '')[:120]}" for m in errors[:3]]}


async def language_check(text: str, language: str) -> Dict[str, Any]:
    async with await _client() as c:
        r = await c.post(os.environ["QC6_LANGUAGETOOL_URL"].rstrip("/") + "/v2/check",
                         data={"text": text, "language": language, "enabledOnly": "false"})
        r.raise_for_status()
        matches = r.json().get("matches", [])
    keep = [m for m in matches if (m.get("rule", {}).get("category", {}).get("id") in ("TYPOS", "GRAMMAR", "CONFUSED_WORDS"))]
    words = []
    for m in keep[:5]:
        ctx = m.get("context", {})
        bad = (ctx.get("text", "")[ctx.get("offset", 0): ctx.get("offset", 0) + ctx.get("length", 0)]).strip()
        fix = (m.get("replacements") or [{}])[0].get("value", "")
        words.append(f'"{bad}" → "{fix}"' if fix else f'"{bad}"')
    return {"mistakes": len(keep), "examples": words}


async def lighthouse(url: str, device: str = "mobile") -> Dict[str, Any]:
    cmd = os.environ["QC6_LIGHTHOUSE_CMD"]
    chrome = os.environ.get("QC6_CHROME_PATH") or await _playwright_chromium()
    fd, out = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd, url, "--quiet", "--output=json", f"--output-path={out}", "--only-categories=performance,accessibility,seo",
            "--chrome-flags=--headless=new --no-sandbox", f"--form-factor={device}",
            *(["--screenEmulation.disabled"] if device == "desktop" else []),
            env={**os.environ, "CHROME_PATH": chrome}, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        with open(out, encoding="utf-8") as f:
            d = json.load(f)
    finally:
        os.unlink(out)
    cats = {k: round((v.get("score") or 0) * 100) for k, v in d.get("categories", {}).items()}
    audits = d.get("audits", {})
    opps = sorted(((a.get("title", k), (a.get("details") or {}).get("overallSavingsMs") or 0) for k, a in audits.items()
                   if (a.get("details") or {}).get("type") == "opportunity"), key=lambda x: -x[1])
    failed_a11y = [audits[r["id"]]["title"] for r in d.get("categories", {}).get("accessibility", {}).get("auditRefs", [])
                   if audits.get(r["id"], {}).get("score") == 0][:3]
    return {"scores": cats, "lcp_ms": (audits.get("largest-contentful-paint") or {}).get("numericValue"),
            "opportunities": [f"{t} (~{round(ms)} ms)" for t, ms in opps[:3] if ms > 0], "a11y_failed": failed_a11y,
            "version": d.get("lighthouseVersion")}


async def _playwright_chromium() -> str:
    """Playwright's own Chromium, so Lighthouse needs no separate browser install."""
    from playwright.async_api import async_playwright
    p = await async_playwright().start()
    try:
        return p.chromium.executable_path
    finally:
        await p.stop()


async def run_extras(pool, run_id: int, site: Dict[str, Any], max_templates: int = 10) -> Dict[str, Any]:
    cfg = configured()
    if not any(cfg.values()):
        return {"extras": "not configured"}
    rows = await pool.fetch("""SELECT DISTINCT ON (p.template_id) p.url, p.template_id, t.name, t.page_count FROM pages p
        LEFT JOIN templates t ON t.id = p.template_id WHERE p.run_id = $1 AND p.http_status BETWEEN 200 AND 299
        AND p.template_id IS NOT NULL ORDER BY p.template_id, p.url""", run_id)
    rows = sorted(rows, key=lambda r: -(r["page_count"] or 0))[:max_templates]
    lang = (site.get("settings") or {}).get("language", "en-US")
    items: List[Item] = []
    report: Dict[str, Any] = {"pages": []}
    for r in rows:
        entry: Dict[str, Any] = {"url": r["url"], "template": r["name"]}
        html = await fetch_html(r["url"], site) if (cfg["nu"] or cfg["languagetool"]) else None
        if html and cfg["nu"]:
            try:
                nu = await nu_check(html)
                entry["nu"] = nu
                if nu["errors"]:
                    items.append(Item("HTML_INVALID", r["url"], "integration", r["template_id"], "",
                                      {"value": nu["errors"], "subject": "; ".join(nu["first"])}, {"nu": nu}))
            except (httpx.HTTPError, ValueError) as e:
                entry["nu_error"] = type(e).__name__
        if html and cfg["languagetool"]:
            try:
                lt = await language_check(visible_text(html), lang)
                entry["languagetool"] = lt
                if lt["mistakes"]:
                    items.append(Item("SPELLING", r["url"], "integration", r["template_id"], "",
                                      {"value": lt["mistakes"], "subject": ", ".join(lt["examples"])}, {"languagetool": lt}))
            except (httpx.HTTPError, ValueError) as e:
                entry["languagetool_error"] = type(e).__name__
        if cfg["lighthouse"]:
            try:
                lh = await lighthouse(r["url"])
                entry["lighthouse"] = lh
                perf, a11y = lh["scores"].get("performance"), lh["scores"].get("accessibility")
                if perf is not None and perf < 90:
                    items.append(Item("LAB_PERFORMANCE", r["url"], "integration", r["template_id"], "",
                                      {"value": perf, "subject": "; ".join(lh["opportunities"]) or "see the report"}, {"lighthouse": lh["scores"]}))
                if a11y is not None and a11y < 90:
                    items.append(Item("LAB_ACCESSIBILITY", r["url"], "integration", r["template_id"], "",
                                      {"value": a11y, "subject": "; ".join(lh["a11y_failed"]) or "see the report"}, {"lighthouse": lh["scores"]}))
            except (asyncio.TimeoutError, OSError, ValueError, KeyError) as e:
                entry["lighthouse_error"] = type(e).__name__
        report["pages"].append(entry)
    from qc6.workers.analysis import apply, close_run
    tpl = {r["template_id"]: r for r in rows}
    applied = await apply(pool, run_id, site["id"], items, template_pages={k: v["page_count"] for k, v in tpl.items()},
                          template_names={k: v["name"] for k, v in tpl.items()}, project_templates=True)
    await close_run(pool, run_id, site["id"], ["integration"], complete=True)
    return {"extras": {k: v for k, v in cfg.items() if v}, "extras_pages": len(rows),
            **{f"extras_{k}": v for k, v in applied.items()}, "extras_report": report}
