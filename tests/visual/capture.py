"""Reproducible visual + structural accessibility verification of the operator dashboard.

Runs inside docker/visual/Dockerfile (pinned Playwright + Chromium + fonts) against the
fake-mode stack already seeded by scripts/simulate.py. Writes desktop and mobile screenshots
plus a JSON report to VISUAL_OUT_DIR and exits non-zero on any structural failure.

Screenshots are review artifacts, not committed baselines. If VISUAL_BASELINE_DIR is set and
contains a PNG of the same name (captured by this same image), a byte-level comparison is
reported; a mismatch fails the run only when VISUAL_STRICT=1.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from playwright.sync_api import Browser, Page, sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
OUT_DIR = Path(os.environ.get("VISUAL_OUT_DIR", "artifacts/visual"))
BASELINE_DIR = os.environ.get("VISUAL_BASELINE_DIR") or None
STRICT = os.environ.get("VISUAL_STRICT") == "1"
OPERATOR_TOKEN = os.environ.get("OPERATOR_TOKEN", "change-me")
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "apache/superset")

# "narrow" approximates a 390px phone with a classic 15px desktop scrollbar taking layout space.
VIEWPORTS = {"desktop": (1440, 900), "mobile": (390, 844), "narrow": (360, 780)}
# Fixture issue numbers exercised by `simulate.py --scenario all`; each lands in a different
# resting state so the case page is captured across the evidence hierarchy.
CASE_ISSUES = {
    "ci-passed": 4702,
    "human-blocked": 4744,
    "probe-infra-blocked": 4748,
    "awaiting-approval": 4222,
    "rejected": 4219,
    "triage-failed": 4633,
    "ci-failed": 4751,
}

A11Y_SCRIPT = """
() => {
  const q = (s) => Array.from(document.querySelectorAll(s));
  const focusables = q('a[href], button, input, select, textarea, summary').filter(
    (el) => !el.hasAttribute('hidden') && el.offsetParent !== null);
  const small = focusables.filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && (r.width < 40 || r.height < 40)
      && el.tagName !== 'A' /* inline text links are exempt from the 40px rule */;
  }).map((el) => `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}`
    + `:${el.textContent.trim().slice(0, 30)}`);
  const tables = q('table').map((t) => ({
    caption: !!t.querySelector('caption'),
    thead: !!t.querySelector('thead'),
    scopedHeaders: t.querySelectorAll('thead th').length > 0
      && Array.from(t.querySelectorAll('thead th'))
        .every((th) => th.getAttribute('scope') === 'col'),
  }));
  const badges = q('.badge').filter((b) => !b.querySelector('.sr-only') && !b.textContent.trim());
  return {
    overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    lang: document.documentElement.lang,
    viewport: !!document.querySelector('meta[name=viewport]'),
    h1: q('h1').length,
    main: q('main#main').length,
    header: q('header').length,
    skipLink: !!document.querySelector('a.skip-link[href="#main"]'),
    tables,
    badTables: tables.filter((t) => !(t.caption && t.thead && t.scopedHeaders)).length,
    unlabelledInputs: q('input:not([type=hidden])').filter(
      (i) => !i.labels?.length && !i.getAttribute('aria-label')).length,
    emptyBadges: badges.length,
    smallTargets: small,
    externalAssets: q('link[rel=stylesheet][href^="http"], script[src^="http"]').length,
    pulsingAnimations: q('*').filter((el) => {
      const a = getComputedStyle(el);
      return a.animationIterationCount === 'infinite';
    }).length,
    focusRing: (() => {
      const first = focusables[0];
      if (!first) return null;
      first.focus();
      const s = getComputedStyle(first);
      return s.outlineStyle !== 'none' || s.boxShadow !== 'none';
    })(),
  };
}
"""


@dataclass
class Report:
    pages: dict[str, dict] = field(default_factory=dict)
    screenshots: dict[str, str] = field(default_factory=dict)
    baseline: dict[str, str] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


def _login(page: Page) -> None:
    page.goto(f"{BASE_URL}/login")
    page.fill("#token", OPERATOR_TOKEN)
    page.click("button[type=submit]")
    page.wait_for_url(f"{BASE_URL}/")


def _case_ids() -> dict[str, str]:
    ids: dict[str, str] = {}
    headers = {"Authorization": f"Bearer {OPERATOR_TOKEN}", "Accept": "application/json"}
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        for label, number in CASE_ISSUES.items():
            response = client.get(f"/api/cases/{REPOSITORY}/{number}", headers=headers)
            if response.status_code == 200:
                ids[label] = response.json()["id"]
    return ids


def _check(report: Report, name: str, a11y: dict, *, mobile: bool) -> None:
    def fail(message: str) -> None:
        report.failures.append(f"{name}: {message}")

    if a11y["overflowX"] > 0:
        fail(f"page overflows the viewport horizontally by {a11y['overflowX']}px")
    if a11y["lang"] != "en":
        fail("html lang is not 'en'")
    if not a11y["viewport"]:
        fail("missing viewport meta")
    if a11y["h1"] != 1:
        fail(f"expected exactly one <h1>, found {a11y['h1']}")
    if a11y["main"] != 1 or not a11y["skipLink"]:
        fail("missing main#main landmark or skip link")
    if a11y["badTables"]:
        fail(f"{a11y['badTables']} table(s) without caption/thead/scope=col")
    if a11y["unlabelledInputs"]:
        fail(f"{a11y['unlabelledInputs']} unlabelled input(s)")
    if a11y["emptyBadges"]:
        fail(f"{a11y['emptyBadges']} badge(s) with no text")
    if a11y["externalAssets"]:
        fail("external stylesheet/script detected")
    if a11y["pulsingAnimations"]:
        fail("infinite animation detected")
    if a11y["focusRing"] is False:
        fail("first focusable element has no visible focus ring")
    if mobile and a11y["smallTargets"]:
        fail(f"controls under 40x40px on mobile: {a11y['smallTargets'][:5]}")


def _capture(browser: Browser, report: Report, name: str, path: str, *, logged_in: bool) -> None:
    for viewport, (width, height) in VIEWPORTS.items():
        context = browser.new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=1,
            reduced_motion="reduce",
            color_scheme="light",
            locale="en-US",
            timezone_id="UTC",
        )
        page = context.new_page()
        if logged_in:
            _login(page)
        page.goto(f"{BASE_URL}{path}")
        page.wait_for_load_state("networkidle")
        # Freeze relative clocks so two captures of the same state look identical.
        page.add_style_tag(content="time { visibility: hidden !important; }")
        key = f"{name}-{viewport}"
        target = OUT_DIR / f"{key}.png"
        page.screenshot(path=str(target), full_page=True)
        page.screenshot(path=str(OUT_DIR / f"{key}-fold.png"))
        report.screenshots[key] = str(target)
        a11y = page.evaluate(A11Y_SCRIPT)
        report.pages[key] = a11y
        _check(report, key, a11y, mobile=viewport != "desktop")
        if BASELINE_DIR:
            baseline = Path(BASELINE_DIR) / f"{key}.png"
            if baseline.exists():
                same = (
                    hashlib.sha256(baseline.read_bytes()).digest()
                    == hashlib.sha256(target.read_bytes()).digest()
                )
                report.baseline[key] = "match" if same else "differs"
                if not same and STRICT:
                    report.failures.append(f"{key}: differs from baseline")
            else:
                report.baseline[key] = "no baseline"
        context.close()


def wait_for_api(timeout: float = 120.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{BASE_URL}/health", timeout=5).status_code == 200:
                return 0
        except httpx.HTTPError:
            pass
        time.sleep(1)
    print(f"api at {BASE_URL} not healthy after {timeout:.0f}s")
    return 1


def main() -> int:
    if "--wait-for-api" in sys.argv[1:]:
        return wait_for_api()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = Report()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        _capture(browser, report, "login", "/login", logged_in=False)
        _capture(browser, report, "dashboard", "/", logged_in=True)
        ids = _case_ids()
        if not ids:
            report.failures.append("no fixture cases found; was simulate.py run?")
        for label, case_id in ids.items():
            _capture(browser, report, f"case-{label}", f"/cases/{case_id}", logged_in=True)
        browser.close()
    (OUT_DIR / "report.json").write_text(json.dumps(asdict(report), indent=2))
    for key in sorted(report.screenshots):
        print(f"  [shot] {report.screenshots[key]}")
    for failure in report.failures:
        print(f"  [FAIL] {failure}")
    if report.failures:
        print(f"{len(report.failures)} structural check(s) failed")
        return 1
    print(f"all structural checks passed across {len(report.pages)} page captures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
