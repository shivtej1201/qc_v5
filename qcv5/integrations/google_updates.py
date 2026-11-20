"""Confirmed Google Search updates (core, spam, policy, product) since March 2024, each citing a Google-owned URL.

Offline dataset bundled from claude-seo's data/google-updates.json (MIT, (c) 2026 agricidaniel). Use it to line up
traffic changes with update dates. Third-party (unverified) claims are kept apart and never mixed in.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "google_updates.json")


def load() -> Dict[str, Any]:
    with open(_DATA, encoding="utf-8") as f:
        return json.load(f)


def query(since: Optional[str] = None, kind: Optional[str] = None, limit: int = 50,
          include_unverified: bool = False) -> Dict[str, Any]:
    d = load()
    items = sorted(d.get("updates", []), key=lambda u: u.get("date", ""), reverse=True)
    if since:
        items = [u for u in items if u.get("date", "") >= since]
    if kind:
        k = kind.lower()
        items = [u for u in items if k in str(u.get("kind", "")).lower()]
    total = len(items)
    if limit and limit > 0:
        items = items[:limit]
    kinds = sorted({p for u in d.get("updates", []) for p in str(u.get("kind", "")).split("+") if p})
    out = {"last_verified": d.get("last_verified"), "source_of_truth": d.get("source_of_truth"),
           "total_matching": total, "truncated": total > len(items), "kinds": kinds, "updates": items}
    if include_unverified:
        out["unverified"] = d.get("unverified", [])
    return out
