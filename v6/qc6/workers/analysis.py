"""Findings -> fixes -> lifecycle -> score. Shared by every worker (blueprint §5 step 7-8, §10.4)."""
from __future__ import annotations

import json
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from qc6.domain import fixes as fx
from qc6.domain import rules as R
from qc6.domain import score as S


@dataclass
class Item:
    rule: str
    page_url: str
    source: str                         # coverage | visit | journey | integration
    template_id: Optional[int] = None
    subject: str = ""                   # part of the fix key (broken target, a11y rule id …)
    fields: Dict[str, Any] = field(default_factory=dict)    # rendering values (value, subject text …)
    evidence: Dict[str, Any] = field(default_factory=dict)


async def notify(conn, run_id: int, event: str, **data) -> None:
    payload = json.dumps({"run": run_id, "event": event, **data}, default=str)[:7900]
    await conn.execute("SELECT pg_notify('qc6', $1)", payload)


async def apply(pool, run_id: int, site_id: int, items: Iterable[Item],
                template_pages: Optional[Dict[int, int]] = None, template_names: Optional[Dict[int, str]] = None,
                project_templates: bool = False) -> Dict[str, int]:
    """Upsert fixes for these findings. With project_templates=True (sampled visits), a template-scoped fix that was
    seen on at least half of a template's sampled visits counts every page of that template as affected."""
    template_pages = template_pages or {}
    template_names = template_names or {}
    groups: Dict[str, Dict[str, Any]] = {}
    for it in items:
        rule = R.RULES[it.rule]
        path = urllib.parse.urlsplit(it.page_url).path or "/"
        key = fx.fix_key(it.rule, rule.scope, it.template_id, it.subject, path)
        g = groups.setdefault(key, {"item": it, "pages": set(), "rows": []})
        g["pages"].add(it.page_url)
        g["rows"].append(it)
    new = updated = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for key, g in groups.items():
                it: Item = g["item"]
                rule = R.RULES[it.rule]
                n = len(g["pages"])
                if project_templates and rule.scope == "template" and it.template_id in template_pages:
                    n = max(n, template_pages[it.template_id])   # caller passes only findings seen on >= half the sample
                tname = template_names.get(it.template_id, "") if it.template_id is not None else ""
                texts = R.render(it.rule, n_pages=n, template=tname or "matching", example=it.page_url,
                                 **{"subject": it.subject, **it.fields})
                row = await conn.fetchrow(
                    """INSERT INTO fixes (site_id, fix_key, rule, severity, dimension, scope, template_id, subject, title,
                                          pages_affected, first_seen_run, last_seen_run, source, texts, example_url)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$11,$12,$13,$14)
                       ON CONFLICT (site_id, fix_key) DO UPDATE SET
                         title = EXCLUDED.title, pages_affected = EXCLUDED.pages_affected, last_seen_run = EXCLUDED.last_seen_run,
                         texts = EXCLUDED.texts, example_url = EXCLUDED.example_url, updated_at = now()
                       RETURNING id, (xmax = 0) AS inserted""",
                    site_id, key, it.rule, rule.severity, rule.dimension, rule.scope, it.template_id, it.subject,
                    texts["title"], n, run_id, it.source, texts, it.page_url)
                if row["inserted"]:
                    new += 1
                    await conn.execute("INSERT INTO fix_events (fix_id, kind, run_id, detail) VALUES ($1,'found',$2,$3)",
                                       row["id"], run_id, {"pages": n})
                else:
                    updated += 1
                await conn.executemany(
                    "INSERT INTO findings (run_id, fix_id, page_url, subject, evidence, source) VALUES ($1,$2,$3,$4,$5,$6) "
                    "ON CONFLICT DO NOTHING",
                    [(run_id, row["id"], r.page_url, r.subject, r.evidence, r.source) for r in g["rows"][:5000]])
    return {"fixes_new": new, "fixes_updated": updated, "findings": sum(len(g["rows"]) for g in groups.values())}


async def close_run(pool, run_id: int, site_id: int, sources: List[str], complete: bool = True) -> Dict[str, int]:
    """Lifecycle for fixes of the given sources after a run. Only a complete run can verify a fix."""
    moved = defaultdict(int)
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch("SELECT id, status, last_seen_run FROM fixes WHERE site_id = $1 AND source = ANY($2::text[])",
                                    site_id, sources)
            for r in rows:
                seen = r["last_seen_run"] == run_id
                if not seen and not complete:
                    continue
                new_status, regressed = fx.after_run(r["status"], seen)
                if new_status != r["status"] or regressed:
                    await conn.execute("UPDATE fixes SET status = $2, regressed = $3, updated_at = now() WHERE id = $1",
                                       r["id"], new_status, regressed)
                    kind = "regressed" if regressed else ("verified" if new_status == "verified" else "status")
                    await conn.execute("INSERT INTO fix_events (fix_id, kind, run_id, detail) VALUES ($1,$2,$3,$4)",
                                       r["id"], kind, run_id, {"from": r["status"], "to": new_status})
                    moved[kind] += 1
    return dict(moved)


async def snapshot_score(pool, run_id: int, site_id: int, pages_total: int) -> Dict[str, Any]:
    async with pool.acquire() as conn:
        open_fixes = await conn.fetch("SELECT severity, dimension, pages_affected FROM fixes WHERE site_id = $1 "
                                      "AND status IN ('open', 'in_progress', 'fixed_pending_check')", site_id)
        sc = S.compute(pages_total, [dict(r) for r in open_fixes])
        if sc["score"] is not None:
            await conn.execute("INSERT INTO score_snapshots (site_id, run_id, formula, score, dimensions) VALUES ($1,$2,$3,$4,$5) "
                               "ON CONFLICT (run_id, formula) DO UPDATE SET score = EXCLUDED.score, dimensions = EXCLUDED.dimensions",
                               site_id, run_id, sc["formula"], sc["score"], sc["dimensions"])
    return sc
