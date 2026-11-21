"""Turn a Playwright codegen recording into a QC journey (blueprint §8.3).

Record a journey by clicking through the site in a browser with the free Playwright recorder:

    npx playwright codegen --target python https://www.example.com

and paste the generated code. Understood lines (anything else is reported, not guessed):

    page.goto("https://…/path")
    page.get_by_role("button", name="Continue").click()
    page.get_by_label("Monthly income").fill("85000")      (also .click() then .fill())
    page.get_by_label("City").select_option("Pune")
    page.get_by_text("You are eligible").click()            -> an expectation that the text is visible
    expect(page.get_by_text("You are eligible")).to_be_visible()

Clicks on buttons that look irreversible (Submit, Pay …) become `stop_before`, and the journey ends there.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, List, Tuple

import yaml

from qc6.workers.journey import IRREVERSIBLE, parse

_S = r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\''
GOTO = re.compile(rf"page\.goto\(\s*(?:{_S})")
ROLE = re.compile(rf"get_by_role\(\s*(?:{_S})\s*,\s*name\s*=\s*(?:{_S})(?:\s*,\s*exact\s*=\s*True)?\s*\)")
LABEL = re.compile(rf"get_by_label\(\s*(?:{_S})")
PLACEHOLDER = re.compile(rf"get_by_placeholder\(\s*(?:{_S})")
TEXT = re.compile(rf"get_by_text\(\s*(?:{_S})")
FILL = re.compile(rf"\.fill\(\s*(?:{_S})\s*\)")
SELECT = re.compile(rf"\.select_option\(\s*(?:{_S})\s*\)")


def _s(m: re.Match, i: int = 1) -> str:
    v = m.group(i) if m.group(i) is not None else m.group(i + 1)
    return bytes(v, "utf-8").decode("unicode_escape") if "\\" in v else v


def _target(line: str) -> Tuple[str, str]:
    """(selector, kind) for the element a line acts on."""
    m = ROLE.search(line)
    if m:
        role, name = _s(m, 1), _s(m, 3)
        q = "'" if "'" not in name else '"'
        return f"role={role}[name={q}{name}{q}]", role
    m = LABEL.search(line)
    if m:
        return f"label={_s(m)}", "field"
    m = PLACEHOLDER.search(line)
    if m:
        return f"[placeholder=\"{_s(m)}\"]", "field"
    m = TEXT.search(line)
    if m:
        return f"text={_s(m)}", "text"
    return "", ""


def convert(code: str, name: str, device: str = "desktop", base_url: str = "") -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []
    skipped: List[str] = []
    origin = ""
    ended = False
    for raw in code.splitlines():
        line = raw.strip()
        if ended or not line or line.startswith(("#", "import", "from", "def ", "with ", "browser", "context", "playwright",
                                                 "run(", "page = ", "page.close", "context.close", "browser.close")):
            continue
        m = GOTO.search(line)
        if m:
            url = _s(m)
            u = urllib.parse.urlsplit(url)
            origin = origin or f"{u.scheme}://{u.netloc}"
            path = urllib.parse.urlunsplit(("", "", u.path or "/", u.query, "")) if f"{u.scheme}://{u.netloc}" == origin else url
            steps.append({"open": path})
            continue
        sel, kind = _target(line)
        if not sel:
            skipped.append(line)
            continue
        if line.startswith("expect(") and ".to_be_visible()" in line:
            _attach_expect(steps, sel)
            continue
        fm, sm = FILL.search(line), SELECT.search(line)
        if fm:
            if steps and "fill" in steps[-1] and "expect" not in steps[-1]:
                steps[-1]["fill"][sel] = _s(fm)
            else:
                steps.append({"fill": {sel: _s(fm)}})
        elif sm:
            steps.append({"select": {sel: _s(sm)}})
        elif line.endswith(".click()") or ".click(" in line:
            if kind == "field":
                continue                     # clicking into a field before typing: implied by fill
            if kind == "text":
                _attach_expect(steps, sel)   # codegen records "assert this text" as a click on it
                continue
            nm = re.search(r"name=['\"]([^'\"]+)", sel)
            if nm and IRREVERSIBLE.search(nm.group(1)):
                steps.append({"stop_before": sel})
                ended = True
                continue
            steps.append({"click": sel})
        else:
            skipped.append(line)
    spec = {"name": name, "device": device, "steps": steps}
    text = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=120)
    parse(text)                              # raises SpecError with a readable message if something is off
    return {"yaml": text, "skipped": skipped, "origin": origin}


def _attach_expect(steps: List[Dict[str, Any]], sel: str) -> None:
    if not steps:
        return
    steps[-1].setdefault("expect", []).append({"visible": sel})
