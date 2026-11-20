"""Fix grouping and lifecycle (blueprint §10.4).

Findings that share a root cause share a fix key and become one Fix. Fixes persist across runs; findings are
immutable per run. After a complete run:
  * a fix with findings that was verified / fixed_pending_check / wont_fix-not  -> 'open', regressed if it was verified
  * an open/in_progress/fixed_pending_check fix with no findings                -> 'verified'
"""
from __future__ import annotations

import hashlib
from typing import Optional

from .rules import RULES
from .templates import path_pattern


def fix_key(rule: str, scope: str, template_id: Optional[int], subject: str, url_path: str) -> str:
    if scope == "template":
        basis = f"t:{template_id}" if template_id is not None else f"p:{path_pattern(url_path)}"
        if subject:                       # e.g. one fix per (template, accessibility rule)
            basis += f"|{subject}"
    elif scope == "subject":
        basis = f"s:{subject}"
    else:
        basis = f"u:{url_path}|{subject}"
    return hashlib.sha1(f"{rule}|{basis}".encode()).hexdigest()[:16]


def key_for(rule: str, template_id: Optional[int], subject: str, url_path: str) -> str:
    return fix_key(rule, RULES[rule].scope, template_id, subject, url_path)


TRANSITIONS = {
    "open": {"in_progress", "fixed_pending_check", "wont_fix"},
    "in_progress": {"open", "fixed_pending_check", "wont_fix"},
    "fixed_pending_check": {"open", "in_progress", "wont_fix"},
    "verified": {"open"},
    "wont_fix": {"open"},
}


def can_move(src: str, dst: str) -> bool:
    """People can't mark something verified; only a run can (it needs evidence)."""
    return dst in TRANSITIONS.get(src, set())


def after_run(status: str, seen: bool) -> tuple[str, bool]:
    """(new_status, regressed) for one fix after a complete run."""
    if seen:
        if status == "verified":
            return "open", True
        if status == "fixed_pending_check":
            return "open", False          # someone said fixed, the run disagrees
        return status, False
    if status in ("open", "in_progress", "fixed_pending_check"):
        return "verified", False
    return status, False
