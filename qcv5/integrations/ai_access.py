"""AI crawler access: robots.txt rules per AI user agent, Content-Signal preferences and /llms.txt.

robots.txt parsing follows RFC 9309 (groups, longest-match, allow wins ties, product-token matching as Google
documents it). The parser, agent list and Content-Signal rules are ported from claude-seo's agentic_check.py
(MIT, (c) 2026 agricidaniel). No API key needed.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .base import IntegrationResult, SafeFetcher, finding, normalize_target, site_root

ID = "ai_access"

# token, vendor, role, documented robots behaviour
AI_AGENTS = [
    ("GPTBot", "OpenAI", "training", "honours"),
    ("OAI-SearchBot", "OpenAI", "search", "honours"),
    ("ChatGPT-User", "OpenAI", "user", "may not apply"),
    ("ClaudeBot", "Anthropic", "training", "honours"),
    ("Claude-SearchBot", "Anthropic", "search", "honours"),
    ("Claude-User", "Anthropic", "user", "honours"),
    ("PerplexityBot", "Perplexity", "search", "honours"),
    ("Perplexity-User", "Perplexity", "user", "generally ignores"),
    ("Google-Extended", "Google", "control", "honours"),
    ("Applebot-Extended", "Apple", "control", "honours"),
    ("CCBot", "Common Crawl", "training", "honours"),
    ("Googlebot", "Google", "search", "honours"),
    ("Bingbot", "Microsoft", "search", "honours"),
]
ROLE_MEANING = {
    "training": "model training",
    "search": "search / AI answer indexing and citation",
    "user": "fetches a person asked for",
    "control": "robots.txt control token (no separate crawler)",
}
CONTENT_SIGNAL_KEYS = {"search", "ai-input", "ai-train"}
CONTENT_SIGNAL_EXPERIMENTAL = {"use": {"immediate", "reference", "full"}}

_LINE_BREAK = re.compile(r"\r\n|\n|\r")
_PRODUCT_TOKEN = re.compile(r"[A-Za-z_-]+")
_STAR_RUN = re.compile(r"\*{2,}")


def robots_lines(text: str) -> List[str]:
    """Split on CR, LF or CRLF only (RFC 9309); str.splitlines would also split on U+2028 inside comments."""
    lines = _LINE_BREAK.split(text.lstrip("﻿"))
    if lines and lines[-1] == "" and _LINE_BREAK.search(text[-2:] if text else ""):
        lines.pop()
    return lines


def parse_robots(text: str) -> Dict[str, Any]:
    groups: List[Dict[str, Any]] = []
    global_lines: Dict[str, List[str]] = {"sitemap": [], "orphan_content_signal": []}
    current = None
    last_was_ua = False
    for raw in robots_lines(text or ""):
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        fld, value = (part.strip() for part in line.split(":", 1))
        fld = fld.lower()
        if fld == "user-agent":
            if current is None or not last_was_ua:
                current = {"agents": [], "rules": [], "content_signal": [], "other": []}
                groups.append(current)
            current["agents"].append(value)
            last_was_ua = True
            continue
        last_was_ua = False
        if fld == "sitemap":
            global_lines["sitemap"].append(value)
        elif current is None:
            if fld == "content-signal":
                global_lines["orphan_content_signal"].append(value)
        elif fld in ("allow", "disallow"):
            current["rules"].append((fld, value))
        elif fld == "content-signal":
            current["content_signal"].append(value)
        else:
            current["other"].append((fld, value))
    return {"groups": groups, **global_lines}


def product_token(value: str) -> str:
    value = (value or "").strip()
    if value == "*":
        return "*"
    m = _PRODUCT_TOKEN.match(value)
    return m.group(0).lower() if m else ""


def _group_names(group: Dict[str, Any], token_l: str) -> bool:
    return any(product_token(a) == token_l for a in group["agents"])


def select_group(parsed: Dict[str, Any], token: str) -> Dict[str, Any]:
    """RFC 9309: merge every group naming the token; otherwise every '*' group."""
    token_l = token.strip().lower()
    named = [g for g in parsed["groups"] if _group_names(g, token_l)]
    chosen = named or [g for g in parsed["groups"] if _group_names(g, "*")]
    return {"matched": "named" if named else ("star" if chosen else "none"),
            "rules": [r for g in chosen for r in g["rules"]],
            "content_signal": [c for g in chosen for c in g["content_signal"]]}


def _pattern_matches(pattern: str, path: str) -> bool:
    """'*' = any run, trailing '$' = end. str.find, not a regex built from untrusted rules (no backtracking)."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    if "*" not in body:
        return path == body if anchored else path.startswith(body)
    first, *middle, last = _STAR_RUN.sub("*", body).split("*")
    if not path.startswith(first):
        return False
    pos = len(first)
    for piece in middle:
        found = path.find(piece, pos)
        if found < 0:
            return False
        pos = found + len(piece)
    if anchored:
        return len(path) - len(last) >= pos and path.endswith(last)
    return path.find(last, pos) >= 0


def is_allowed(rules: List, path: str = "/") -> bool:
    """Longest match wins; allow wins a tie; an empty Disallow allows everything."""
    best_len, allowed = -1, True
    for fld, value in rules:
        if fld == "disallow" and value == "":
            continue
        if _pattern_matches(value, path):
            length = len(value)
            if length > best_len or (length == best_len and fld == "allow"):
                best_len, allowed = length, fld == "allow"
    return allowed


def parse_content_signal(value: str) -> Dict[str, Any]:
    signals, issues = {}, []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            issues.append(f"malformed entry '{part}'")
            continue
        key, val = (s.strip().lower() for s in part.split("=", 1))
        signals[key] = val
        if key in CONTENT_SIGNAL_EXPERIMENTAL:
            if val not in CONTENT_SIGNAL_EXPERIMENTAL[key]:
                issues.append(f"value for '{key}' must be one of {sorted(CONTENT_SIGNAL_EXPERIMENTAL[key])}")
            continue
        if key not in CONTENT_SIGNAL_KEYS:
            issues.append(f"unknown key '{key}'")
        elif val not in ("yes", "no"):
            issues.append(f"value for '{key}' must be yes or no")
    return {"signals": signals, "issues": issues}


_LLMS_LINK = re.compile(r"^\s*[-*+]\s*\[[^\]]+\]\([^)\s]+\)")


def evaluate_llms_txt(status: Optional[int], content: Optional[str]) -> Dict[str, Any]:
    """llms.txt (llmstxt.org community proposal, not a ratified standard): an H1 is the only required part."""
    if status is None or status >= 400 or content is None:
        return {"present": False, "status": status}
    lines = [l.rstrip() for l in content.splitlines()]
    h1 = next((l[2:].strip() for l in lines if l.startswith("# ")), None)
    sections = [l[3:].strip() for l in lines if l.startswith("## ")]
    links = sum(1 for l in lines if _LLMS_LINK.match(l))
    looks_html = content.lstrip()[:200].lower().startswith(("<!doctype", "<html"))
    return {"present": True, "status": status, "bytes": len(content.encode("utf-8")), "h1": h1,
            "has_summary": any(l.startswith(">") for l in lines), "sections": sections[:30], "links": links,
            "looks_like_html": looks_html}


async def run(url: str, **_: Any) -> Dict[str, Any]:
    target = normalize_target(url)
    root = site_root(target)
    res = IntegrationResult(ID, target, source="claude-seo agentic_check.py (robots/llms.txt), RFC 9309")
    async with SafeFetcher() as f:
        robots = await f.get(root + "/robots.txt", max_body=512_000)
        llms = await f.get(root + "/llms.txt", max_body=512_000)

    if robots.error or robots.status is None:
        res.status = "unreachable"
        res.error = robots.error
        res.findings.append(finding("robots-unreachable", "HIGH", "robots.txt could not be fetched",
                                    robots.error or "", "Make /robots.txt reachable for crawlers."))
        return res.as_dict()

    data: Dict[str, Any] = {"robots_status": robots.status}
    if robots.status == 429:
        res.findings.append(finding("robots-rate-limited", "MEDIUM", "robots.txt answered 429 (rate limited)",
                                    "Google treats a 429 on robots.txt as a server error, not as 'no rules'.",
                                    "Do not rate-limit /robots.txt for crawlers."))
        parsed = parse_robots("")
    elif robots.status >= 500:
        res.findings.append(finding("robots-5xx", "HIGH", f"robots.txt returns {robots.status}",
                                    "A 5xx robots.txt makes compliant crawlers treat the whole site as disallowed.",
                                    "Fix the server error on /robots.txt."))
        parsed = parse_robots("")
    elif robots.status >= 400:
        res.findings.append(finding("robots-missing", "LOW", "No robots.txt",
                                    "Everything is allowed and no AI-usage preference is declared.",
                                    "Publish a deliberate robots.txt."))
        parsed = parse_robots("")
    else:
        parsed = parse_robots(robots.text)

    agents = []
    for token, vendor, role, behaviour in AI_AGENTS:
        sel = select_group(parsed, token)
        agents.append({"token": token, "vendor": vendor, "role": role, "governs": ROLE_MEANING[role],
                       "robots_behaviour": behaviour, "group": sel["matched"],
                       "root_allowed": is_allowed(sel["rules"], "/"), "content_signal": sel["content_signal"]})
    data["agents"] = agents
    data["sitemaps_declared"] = parsed["sitemap"][:20]
    data["group_count"] = len(parsed["groups"])

    blocked_search = [a["token"] for a in agents if a["role"] == "search" and not a["root_allowed"]]
    blocked_training = [a["token"] for a in agents if a["role"] in ("training", "control") and not a["root_allowed"]]
    if blocked_search:
        sev = "HIGH" if any(t in ("Googlebot", "Bingbot") for t in blocked_search) else "MEDIUM"
        res.findings.append(finding("search-crawlers-blocked", sev, "Search / AI-answer crawlers blocked at /",
                                    ", ".join(blocked_search),
                                    "Blocking a search crawler removes the site from that engine's results or AI "
                                    "answers. Confirm this is intended.", {"agents": blocked_search}))
    if not parsed["sitemap"] and robots.status and robots.status < 400:
        res.findings.append(finding("no-sitemap-line", "LOW", "robots.txt declares no Sitemap",
                                    fix="Add 'Sitemap: https://…/sitemap.xml' so every crawler finds it."))

    signals = [s for g in parsed["groups"] for s in g["content_signal"]] + parsed["orphan_content_signal"]
    sig_issues = [i for v in signals for i in parse_content_signal(v)["issues"]]
    if parsed["orphan_content_signal"]:
        sig_issues.append("Content-Signal outside any user-agent group")
    data["content_signal"] = {"lines": signals, "issues": sig_issues}
    if sig_issues:
        res.findings.append(finding("content-signal-invalid", "LOW", "Content-Signal lines have problems",
                                    "; ".join(sig_issues[:5]),
                                    "Use 'Content-Signal: search=yes, ai-input=yes, ai-train=no' inside a group."))
    elif not signals:
        res.findings.append(finding("content-signal-absent", "INFO", "No Content-Signal preference declared",
                                    "Optional: states search / ai-input / ai-train preferences (a preference, "
                                    "not enforcement)."))

    llms_eval = evaluate_llms_txt(llms.status if not llms.error else None, llms.text if llms.status else None)
    data["llms_txt"] = llms_eval
    if not llms_eval["present"]:
        res.findings.append(finding("llms-txt-absent", "INFO", "No /llms.txt",
                                    "Optional community proposal (llmstxt.org) that lists key pages for LLMs.",
                                    "Optional: publish /llms.txt with an H1, a > summary and link lists."))
    elif llms_eval["looks_like_html"]:
        res.findings.append(finding("llms-txt-html", "LOW", "/llms.txt returns HTML",
                                    "The URL serves an HTML page (often a soft 404), not Markdown.",
                                    "Serve a Markdown file or return 404."))
    elif not llms_eval["h1"]:
        res.findings.append(finding("llms-txt-no-h1", "LOW", "/llms.txt has no H1",
                                    "The H1 with the site name is the only required part of the proposal.",
                                    "Start the file with '# Site name'."))

    res.data = data
    res.summary = {"robots_status": robots.status, "ai_agents_checked": len(agents),
                   "agents_allowed_at_root": sum(1 for a in agents if a["root_allowed"]),
                   "search_blocked": blocked_search, "training_blocked": blocked_training,
                   "llms_txt": llms_eval["present"]}
    return res.as_dict()
