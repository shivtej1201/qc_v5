"""Journeys in real Chromium against the fixture site."""
import asyncio
import os

import pytest

os.environ["QC6_ALLOW_PRIVATE_TARGETS"] = "true"
from fixture_site import FixtureSite  # noqa: E402
from qc6.workers import journey as J  # noqa: E402

SPEC = """
name: Check loan eligibility
device: {device}
steps:
  - open: {path}
    expect:
      - visible: "role=heading[name=/eligibility/i]"
  - fill:
      "label=Monthly income": "85000"
  - select:
      "label=City": "Pune"
  - click: "role=button[name='Continue']"
    expect:
      - visible: "text=/You are eligible/"
      - no_console_errors: true
  - stop_before: "role=button[name='Submit application']"
"""


@pytest.fixture(scope="module")
def site():
    s = FixtureSite()
    yield s
    s.close()


def _run(site, spec_text, tmp_path):
    from playwright.async_api import async_playwright
    spec = J.parse(spec_text)

    async def go():
        async with async_playwright() as p:
            b = await p.chromium.launch()
            try:
                return await J.JourneyRunner(b, {"base_url": site.base, "access_token": "tok"}, str(tmp_path), fast=True).run(spec)
            finally:
                await b.close()
    return asyncio.run(go())


def test_journey_passes_and_stops_before_submit(site, tmp_path):
    out = _run(site, SPEC.format(device="mobile", path="/apply"), tmp_path)
    assert out.status == "passed", [(s.label, s.status, s.detail) for s in out.steps]
    assert [s.label for s in out.steps] == ["Open /apply", "Type Monthly income", "Choose Pune", 'Tap "Continue"',
                                            'Stop before "Submit application"']
    assert all(s.screenshot for s in out.steps)


def test_journey_failure_is_explained_like_a_person(site, tmp_path):
    out = _run(site, SPEC.format(device="desktop", path="/apply-broken"), tmp_path)
    assert out.status == "failed" and out.failed_step == 4
    assert out.failure_text == ('Tap "Continue": "You are eligible" never appeared. After the tap the page sent nothing '
                                "to the server. A browser error appeared: Cannot read properties of undefined (reading 'city')")
    assert out.steps[-1].status == "skipped"


def test_irreversible_steps_are_refused():
    with pytest.raises(J.SpecError, match="irreversible"):
        J.parse("name: x\nsteps:\n  - click: \"role=button[name='Submit application']\"\n")
    with pytest.raises(J.SpecError, match="exactly one action"):
        J.parse("name: x\nsteps:\n  - click: a\n    open: /b\n")


def test_journeys_cannot_reach_private_addresses(site, tmp_path, monkeypatch):
    """Same SSRF guard as visits: with private targets not allowed, a journey to 127.0.0.1 is blocked."""
    from qc6.settings import settings
    monkeypatch.setattr(settings, "allow_private_targets", False)
    out = _run(site, SPEC.format(device="desktop", path="/apply"), tmp_path)
    assert out.status == "failed" and out.failed_step == 1
    assert "ERR_BLOCKED_BY_CLIENT" in out.failure_text or "blocked" in out.failure_text.lower()
