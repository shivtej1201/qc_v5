"""Schedules, alerts, recorder, tickets, visual change. External services are mocked (no network)."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from PIL import Image, ImageDraw

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"

from qc6 import notify, recorder, scheduler, services, tickets  # noqa: E402
from qc6.domain.visual import changed_ratio  # noqa: E402
from qc6.workers.journey import SpecError, parse  # noqa: E402

UTC = timezone.utc


# ------------------------------------------------------------------ schedules
def test_schedule_parsing_and_next_due():
    assert scheduler.parse("off") is None and scheduler.parse("") is None
    assert scheduler.parse("Daily 06:30") == {"type": "at", "days": list(range(7)), "hour": 6, "minute": 30}
    assert scheduler.parse("weekdays 07:15")["days"] == [0, 1, 2, 3, 4]
    assert scheduler.parse("every 6 hours") == {"type": "every", "minutes": 360}
    for bad in ("every 5 minutes", "daily 25:00", "sometimes"):
        with pytest.raises(scheduler.ScheduleError):
            scheduler.parse(bad)
    # Friday 2026-11-20 10:00 IST -> next "weekdays 07:15" is Monday 23rd 07:15 IST = 01:45 UTC
    last = datetime(2026, 11, 20, 4, 30, tzinfo=UTC)
    assert scheduler.next_due(scheduler.parse("weekdays 07:15"), last) == datetime(2026, 11, 23, 1, 45, tzinfo=UTC)
    assert scheduler.next_due(scheduler.parse("daily 06:30"), last) == datetime(2026, 11, 21, 1, 0, tzinfo=UTC)
    assert scheduler.next_due(scheduler.parse("every 6 hours"), last) == last + timedelta(hours=6)
    with pytest.raises(SpecError, match="daily 06:30"):
        parse("name: x\nschedule: whenever\nsteps:\n  - open: /\n")


def test_scheduler_tick_starts_runs_once(pg_pool):
    async def go():
        pool = await pg_pool()
        ws = await services.create_workspace(pool, "Sched Co", "s@sched.test", "Sam")
        sid = await services.create_site(pool, ws["workspace_id"], "Sched", "https://sched.test",
                                         {"check_schedule": "daily 06:30", "timezone": "Asia/Kolkata"})
        jid = await services.save_journey(pool, sid, "name: Home\nschedule: every 2 hours\nsteps:\n  - open: /\n")
        t0 = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
        first = await scheduler.tick(pool, t0)                     # first sight: start counting, don't fire
        second = await scheduler.tick(pool, t0 + timedelta(hours=1, minutes=5))   # 06:35 IST: daily check due
        busy = await scheduler.tick(pool, t0 + timedelta(hours=2, minutes=5))     # journey due but site busy
        await pool.execute("UPDATE runs SET status = 'completed' WHERE site_id = $1", sid)
        third = await scheduler.tick(pool, t0 + timedelta(hours=2, minutes=6))
        again = await scheduler.tick(pool, t0 + timedelta(hours=2, minutes=7))
        opts = await pool.fetchval("SELECT options FROM runs WHERE site_id = $1 AND kind = 'journey'", sid)
        await pool.close()
        return first, second, busy, third, again, opts, jid
    first, second, busy, third, again, opts, jid = asyncio.run(go())
    assert first == [] and [x["kind"] for x in second] == ["full"] and busy == []
    assert [x["kind"] for x in third] == ["journey"] and again == []
    assert opts["journey_ids"] == [jid] and opts["scheduled"] is True


# ------------------------------------------------------------------ alerts
def test_webhook_validation_and_alert_messages(pg_pool):
    assert notify.valid_webhook("slack", "https://hooks.slack.com/services/T/B/x")
    assert not notify.valid_webhook("slack", "https://hooks.slack.com.evil.test/x")
    assert not notify.valid_webhook("slack", "http://hooks.slack.com/services/x")
    assert notify.valid_webhook("teams", "https://acme.webhook.office.com/webhookb2/x")
    assert not notify.valid_webhook("teams", "https://169.254.169.254/latest")
    posted = []

    def handler(req: httpx.Request):
        posted.append((req.url.host, json.loads(req.content)))
        return httpx.Response(200)
    notify.set_transport(httpx.MockTransport(handler))

    async def go():
        pool = await pg_pool()
        ws = await services.create_workspace(pool, "Alert Co", "a@alert.test", "Ana")
        sid = await services.create_site(pool, ws["workspace_id"], "Alert site", "https://alert.test", {"alerts": {
            "slack_webhook": "https://hooks.slack.com/services/T/B/x", "teams_webhook": "https://acme.webhook.office.com/w/x",
            "on": ["summary", "journey_failed"]}})
        rid = await services.start_run(pool, sid, "journey")
        jid = await services.save_journey(pool, sid, "name: Apply\nsteps:\n  - open: /\n")
        await pool.execute("INSERT INTO journey_runs (journey_id, run_id, device, status, failed_step, failure_text) VALUES "
                           "($1,$2,'mobile','failed',3,'Tap \"Continue\": nothing happened.')", jid, rid)
        site = dict(await pool.fetchrow("SELECT * FROM sites WHERE id = $1", sid))
        sent = await notify.after_run(pool, rid, site, {"journeys": 1, "journeys_passed": 0, "score": 71.4})
        logged = await pool.fetchval("SELECT COUNT(*) FROM alerts_sent WHERE site_id = $1", sid)
        await pool.close()
        return sent, logged
    try:
        sent, logged = asyncio.run(go())
    finally:
        notify.set_transport(None)
    assert {(s["channel"], s["kind"]) for s in sent} == {("slack", "journey_failed"), ("teams", "journey_failed"),
                                                         ("slack", "summary"), ("teams", "summary")}
    assert logged == 4
    slack = [b for h, b in posted if h == "hooks.slack.com"]
    assert slack[0]["text"] == "Alert site: 1 journey failed"
    assert slack[0]["blocks"][1]["text"]["text"] == '• "Apply" at step 3: Tap "Continue": nothing happened.'
    assert any("Health score 71 out of 100" in json.dumps(b) for _, b in posted)


# ------------------------------------------------------------------ recorder
CODEGEN = '''
import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.example.com/personal-loan")
    page.get_by_role("link", name="Check eligibility").click()
    page.get_by_label("Monthly income").click()
    page.get_by_label("Monthly income").fill("85000")
    page.get_by_label("City").select_option("Pune")
    page.get_by_role("button", name="Continue").click()
    page.get_by_text("You are eligible").click()
    expect(page.get_by_role("heading", name="Your offer")).to_be_visible()
    page.get_by_role("button", name="Submit application").click()
    page.get_by_role("button", name="Something after submit").click()
    context.close()
    browser.close()
'''


def test_recorder_converts_codegen():
    out = recorder.convert(CODEGEN, "Eligibility", "mobile")
    spec = parse(out["yaml"])
    assert spec["device"] == "mobile" and out["origin"] == "https://www.example.com"
    assert spec["steps"] == [
        {"open": "/personal-loan"},
        {"click": "role=link[name='Check eligibility']"},
        {"fill": {"label=Monthly income": "85000"}},
        {"select": {"label=City": "Pune"}},
        {"click": "role=button[name='Continue']", "expect": [{"visible": "text=You are eligible"},
                                                              {"visible": "role=heading[name='Your offer']"}]},
        {"stop_before": "role=button[name='Submit application']"},
    ]
    assert out["skipped"] == []


# ------------------------------------------------------------------ tickets
def test_ticket_config_and_creation():
    with pytest.raises(ValueError, match="owner/name"):
        tickets.clean_config({"provider": "github", "repo": "nope"})
    with pytest.raises(ValueError, match="atlassian.net"):
        tickets.clean_config({"provider": "jira", "base_url": "https://jira.internal", "project": "WEB", "email": "a@b.c"})
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, str(req.url), req.headers.get("authorization", "")[:12], json.loads(req.content or b"{}")))
        if req.url.host == "api.github.com" and req.method == "POST":
            return httpx.Response(201, json={"html_url": "https://github.com/acme/web/issues/42", "number": 42})
        if req.url.host == "api.github.com":
            return httpx.Response(200, json={"state": "closed"})
        if req.method == "POST":
            return httpx.Response(201, json={"key": "WEB-7"})
        return httpx.Response(200, json={"fields": {"status": {"statusCategory": {"key": "indeterminate"}}}})
    tickets.set_transport(httpx.MockTransport(handler))
    fix = {"title": "41,200 Product pages have no main heading", "severity": "medium", "pages_affected": 41200,
           "texts": {"title": "41,200 Product pages have no main heading", "why": "w", "fix": "f", "verify": "v"}}
    site = {"name": "Demo"}
    try:
        os.environ["QC6_GITHUB_TOKEN"] = "ghp_test"
        os.environ["QC6_JIRA_TOKEN"] = "jira_test"
        gh = asyncio.run(tickets.create({"provider": "github", "repo": "acme/web"}, fix, site, ["https://x.test/p/1"], "https://qc/#/fixes?fix=1"))
        st = asyncio.run(tickets.state({"provider": "github", "repo": "acme/web"}, gh["key"]))
        jr = asyncio.run(tickets.create({"provider": "jira", "base_url": "https://acme.atlassian.net", "project": "WEB",
                                         "email": "bot@acme.com"}, fix, site, [], "https://qc/#/fixes?fix=1"))
        jst = asyncio.run(tickets.state({"provider": "jira", "base_url": "https://acme.atlassian.net", "project": "WEB",
                                         "email": "bot@acme.com"}, jr["key"]))
    finally:
        tickets.set_transport(None)
        os.environ.pop("QC6_GITHUB_TOKEN", None)
        os.environ.pop("QC6_JIRA_TOKEN", None)
    assert gh == {"url": "https://github.com/acme/web/issues/42", "key": "#42"} and st == "done"
    assert jr == {"url": "https://acme.atlassian.net/browse/WEB-7", "key": "WEB-7"} and jst == "open"
    gh_post = calls[0]
    assert gh_post[1] == "https://api.github.com/repos/acme/web/issues" and gh_post[2] == "Bearer ghp_t"
    assert gh_post[3]["title"] == "[QC] 41,200 Product pages have no main heading" and "**How to fix**" in gh_post[3]["body"]
    assert calls[2][3]["fields"]["project"] == {"key": "WEB"}


# ------------------------------------------------------------------ visual change
def test_visual_change_ratio(tmp_path):
    a, b, c = (str(tmp_path / f"{n}.jpg") for n in "abc")
    img = Image.new("RGB", (412, 900), "white")
    ImageDraw.Draw(img).rectangle((20, 20, 390, 120), fill="black")
    img.save(a, quality=62)
    img.save(b, quality=40)                                   # same page, different compression
    changed = img.copy()
    ImageDraw.Draw(changed).rectangle((0, 300, 412, 700), fill="navy")   # a big new banner
    changed.save(c, quality=62)
    assert changed_ratio(a, b) < 0.01
    assert changed_ratio(a, c) > 0.3


def test_visual_changes_between_checks(pg_pool, tmp_path, monkeypatch):
    from qc6.settings import settings
    from qc6.workers.visit_stage import visual_changes
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    os.makedirs(settings.objects_dir, exist_ok=True)
    base = Image.new("RGB", (412, 900), "white")
    changed = base.copy()
    ImageDraw.Draw(changed).rectangle((0, 200, 412, 800), fill="darkred")

    async def go():
        pool = await pg_pool()
        ws = await services.create_workspace(pool, "Vis Co", "v@vis.test", "Vee")
        sid = await services.create_site(pool, ws["workspace_id"], "Vis", "https://vis.test")
        tpl = {}
        for name in ("Product", "Article"):
            tpl[name] = await pool.fetchval("INSERT INTO templates (site_id, fingerprint, name) VALUES ($1, 1, $2) RETURNING id", sid, f"{name} pages")
        runs = [await pool.fetchval("INSERT INTO runs (site_id, kind, status) VALUES ($1,'visit','completed') RETURNING id", sid) for _ in range(2)]
        for i, rid in enumerate(runs):
            for name in ("Product", "Article"):
                for n in range(3):
                    shot = f"visit-{i}{n}{1 if name == "Product" else 2}.jpg"
                    (changed if (i == 1 and name == "Product") else base).save(os.path.join(settings.objects_dir, shot))
                    await pool.execute("INSERT INTO visits (run_id, page_url, template_id, device, status, screenshot) VALUES ($1,$2,$3,'mobile','ok',$4)",
                                       rid, f"https://vis.test/{name}/{n}", tpl[name], shot)
        out = await visual_changes(pool, runs[1], sid)
        stored = await pool.fetchval("SELECT COUNT(*) FROM visits WHERE run_id = $1 AND visual_change IS NOT NULL", runs[1])
        await pool.close()
        return out, stored
    out, stored = asyncio.run(go())
    assert stored == 6
    assert [x["template"] for x in out] == ["Product pages"] and out[0]["changed"] > 0.5
