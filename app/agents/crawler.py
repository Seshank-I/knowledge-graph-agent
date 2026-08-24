"""
Stage 2 — Crawler.

Playwright over a FIXED list of high-value Cal.com screens (a deliberate
scope cut — no autonomous exploration). Per screen it captures:
  - a full-page screenshot + raw DOM snapshot (saved to data/artifacts/)
  - the interactive elements (buttons, inputs, links, selects) with stable-ish
    selectors
and then batches the elements through the LLM once per screen to assign each
a semantic `label` ("submit booking", "timezone selector", ...).

Flows are declared alongside the screen list, since a fixed screen list means
the screen->flow membership is known up front.

Single-pass by design: a screen that fails to load is logged and skipped,
not retried.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from playwright.async_api import Page, async_playwright
from pydantic import BaseModel

from app.agents.llm import complete_validated
from app.config import settings
from app.models import CrawlResult, Flow, Screen, UIElement

log = logging.getLogger(__name__)

ARTIFACTS_DIR = Path("data/artifacts")

# ---------------------------------------------------------------------------
# The fixed crawl scope. Paths are relative to settings.target_app_base_url.
# auth=True screens need the test-account login first.
# ---------------------------------------------------------------------------
SCREENS: list[dict] = [
    {"id": "screen-login", "name": "Login", "path": "/auth/login", "auth": False},
    {"id": "screen-event-types", "name": "Event Types list", "path": "/event-types", "auth": True},
    {"id": "screen-availability", "name": "Availability", "path": "/availability", "auth": True},
    {"id": "screen-bookings", "name": "Bookings list", "path": "/bookings/upcoming", "auth": True},
    {"id": "screen-settings-profile", "name": "Profile Settings", "path": "/settings/my-account/profile", "auth": True},
    {"id": "screen-booker-landing", "name": "Public profile (event list)", "path": "/{username}", "auth": False},
    {"id": "screen-booking-page", "name": "Public booking page", "path": "/{username}/30min", "auth": False},
]

FLOWS: list[Flow] = [
    Flow(id="flow-manage-event-types", name="Manage event types",
         screen_ids=["screen-login", "screen-event-types"]),
    Flow(id="flow-set-availability", name="Set availability",
         screen_ids=["screen-login", "screen-availability"]),
    Flow(id="flow-book-a-slot", name="Book a slot (invitee)",
         screen_ids=["screen-booker-landing", "screen-booking-page"]),
    Flow(id="flow-review-bookings", name="Review bookings",
         screen_ids=["screen-login", "screen-bookings"]),
]

INTERACTIVE_SELECTOR = (
    "button, a[href], input, select, textarea, [role='button'], [role='link'], "
    "[role='tab'], [role='menuitem'], [role='switch'], [role='checkbox']"
)

LABEL_SYSTEM = """You label UI elements for a QA knowledge graph of the
scheduling app Cal.com. For each element (given its tag, attributes, and
visible text) return a short semantic label describing its PURPOSE from the
user's point of view — e.g. "submit booking", "timezone selector",
"create new event type", "toggle availability for Monday".
Keep labels under 6 words, lowercase. Return one label per input element,
same order, matched by the given element id."""


class _LabeledElement(BaseModel):
    id: str
    label: str


class _LabelBatch(BaseModel):
    labels: list[_LabeledElement]


def _stable_selector(el: dict) -> str:
    """Prefer attributes that survive re-renders: data-testid > id > name >
    tag+text. Cal.com uses data-testid heavily, which is why this crawl gets
    away without real selector synthesis."""
    if el.get("testid"):
        return f"[data-testid=\"{el['testid']}\"]"
    if el.get("dom_id"):
        return f"#{el['dom_id']}"
    if el.get("name"):
        return f"{el['tag']}[name=\"{el['name']}\"]"
    text = (el.get("text") or "").strip()[:40]
    return f"{el['tag']}:has-text(\"{text}\")" if text else el["tag"]


def _element_type(tag: str, role: str | None, input_type: str | None) -> str:
    if tag == "a" or role == "link":
        return "link"
    if tag == "select":
        return "select"
    if tag in ("input", "textarea"):
        return input_type or "input"
    return role or "button"


async def _capture_elements(page: Page, screen_id: str) -> list[UIElement]:
    raw = await page.eval_on_selector_all(
        INTERACTIVE_SELECTOR,
        """els => els.filter(e => e.offsetParent !== null).map(e => ({
            tag: e.tagName.toLowerCase(),
            testid: e.getAttribute('data-testid'),
            dom_id: e.id || null,
            name: e.getAttribute('name'),
            role: e.getAttribute('role'),
            input_type: e.getAttribute('type'),
            text: (e.innerText || e.value || e.placeholder || '').slice(0, 120),
        }))""",
    )
    elements: list[UIElement] = []
    seen: set[str] = set()
    for i, el in enumerate(raw):
        selector = _stable_selector(el)
        if selector in seen:  # dedupe repeated rows (e.g. list items)
            continue
        seen.add(selector)
        slug = re.sub(r"[^a-z0-9]+", "-", (el.get("testid") or el.get("dom_id")
                      or el.get("text") or f"el-{i}").lower()).strip("-")[:50]
        elements.append(UIElement(
            id=f"{screen_id}--{slug or i}",
            screen_id=screen_id,
            selector=selector,
            element_type=_element_type(el["tag"], el.get("role"), el.get("input_type")),
            label="",  # filled by the LLM pass below
            raw_text=(el.get("text") or None),
        ))
    return elements


def _label_elements(screen_name: str, elements: list[UIElement]) -> list[UIElement]:
    if not elements:
        return elements
    payload = [
        {"id": el.id, "tag_type": el.element_type,
         "selector": el.selector, "text": el.raw_text}
        for el in elements
    ]
    batch = complete_validated(
        LABEL_SYSTEM,
        f"Screen: {screen_name}\nElements:\n{json.dumps(payload, indent=2)}",
        _LabelBatch,
    )
    by_id = {l.id: l.label for l in batch.labels}
    for el in elements:
        el.label = by_id.get(el.id) or (el.raw_text or el.element_type).lower()[:60]
    return elements


async def _login(page: Page) -> None:
    await page.goto(f"{settings.target_app_base_url}/auth/login")
    await page.fill("input[name='email']", settings.target_app_email)
    await page.fill("input[name='password']", settings.target_app_password)
    await page.click("button[type='submit']")
    await page.wait_for_url("**/event-types**", timeout=20_000)


async def crawl(username: str | None = None) -> CrawlResult:
    """`username` fills the public-booking-page path template; defaults to the
    test account's email local-part."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    username = username or settings.target_app_email.split("@")[0]

    screens: list[Screen] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        logged_in = False
        for spec in SCREENS:
            path = spec["path"].replace("{username}", username)
            url = f"{settings.target_app_base_url}{path}"
            try:
                if spec["auth"] and not logged_in:
                    await _login(page)
                    logged_in = True
                await page.goto(url, wait_until="networkidle", timeout=30_000)

                await page.screenshot(
                    path=str(ARTIFACTS_DIR / f"{spec['id']}.png"), full_page=True)
                (ARTIFACTS_DIR / f"{spec['id']}.html").write_text(await page.content())

                elements = await _capture_elements(page, spec["id"])
                elements = _label_elements(spec["name"], elements)
                screens.append(Screen(
                    id=spec["id"], url=url, name=spec["name"], elements=elements))
                log.info("crawled %s: %d elements", spec["id"], len(elements))
            except Exception:
                # Single-pass pipeline: log and move on, no retry loops.
                log.exception("failed to crawl %s (%s) — skipping", spec["id"], url)

        await browser.close()

    crawled_ids = {s.id for s in screens}
    flows = [
        Flow(id=f.id, name=f.name,
             screen_ids=[sid for sid in f.screen_ids if sid in crawled_ids])
        for f in FLOWS
    ]
    flows = [f for f in flows if f.screen_ids]
    return CrawlResult(screens=screens, flows=flows)
