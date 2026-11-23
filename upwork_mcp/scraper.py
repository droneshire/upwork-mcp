"""Drives the user's live Chrome (attached over CDP) to read Upwork pages.

Parsing deliberately avoids CSS class names: it finds links by URL pattern
(/jobs/, /messages/rooms/, /proposals/) and takes the surrounding container's
text, with a few optional data-test hooks. Raw text is always kept so data is
still useful when Upwork changes its markup.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
from urllib.parse import quote_plus

from playwright.async_api import Browser, Page, async_playwright

from .config import settings

BASE = "https://www.upwork.com"
FEEDS = {
    "best_matches": f"{BASE}/nx/find-work/best-matches",
    "most_recent": f"{BASE}/nx/find-work/most-recent",
}
MESSAGES_URL = f"{BASE}/ab/messages/rooms/"
PROPOSALS_URL = f"{BASE}/nx/proposals/"

_JOB_TILES_JS = r"""
() => {
  const abs = h => new URL(h, location.origin).href.split('?')[0];
  const seen = new Set(), out = [];
  const links = [...document.querySelectorAll('a[href*="/jobs/"]')]
    .filter(a => /~0[0-9a-z]+/i.test(a.getAttribute('href')) && a.innerText.trim());
  for (const a of links) {
    const url = abs(a.getAttribute('href'));
    if (seen.has(url)) continue;
    seen.add(url);
    // Search tiles are <article data-test="JobTile"> with inner <section>s; feed tiles are <section>.
    const tile = a.closest('article')
              || a.closest('section, [data-test*="job-tile" i], [data-ev-sublocation*="job" i]')
              || a.parentElement?.parentElement?.parentElement || a;
    // Ignore the "not interested" feedback popper, whose options contain words like "budget".
    const q = sel => [...tile.querySelectorAll(sel)]
      .find(e => !e.closest('[data-test="popper"]'))?.innerText?.trim().replace(/\s+/g, ' ') || null;
    // Two layouts: Find Work feeds (posted-on, job-type, ...) and search (JobTile article).
    const budget = [q('[data-test="job-type"], [data-test="job-type-label"]'),
                    q('[data-test="is-fixed-price"], [data-test="budget"]'),
                    q('[data-test="contractor-tier"], [data-test="experience-level"]'),
                    q('[data-test="duration"], [data-test="duration-label"]')]
                   .filter(Boolean).join(' · ') || null;
    out.push({
      url,
      title: a.innerText.trim(),
      posted: q('[data-test="posted-on"], [data-test="job-pubilshed-date"], [data-test*="published" i]'),
      budget,
      description: q('[data-test="job-description-text"], [data-test*="JobDescription"], [data-test*="job-description" i]'),
      skills: [...new Set([...tile.querySelectorAll(
                 '[data-test="attr-item"], [data-test="token"], [data-test*="JobAttrs"] button')]
                .map(e => e.innerText.trim()).filter(Boolean))].slice(0, 20),
      meta: {
        proposals: q('[data-test="proposals-tier"]'),
        payment_verified: q('[data-test="payment-verification-status"], [data-test="payment-verified"]'),
        client_rating: q('[data-test="js-feedback"], [data-test="total-feedback"]'),
        client_spend: q('[data-test="client-spendings"], [data-test="total-spent"]'),
        client_country: q('[data-test="client-country"], [data-test="location"]'),
      },
      tile_text: tile.innerText.trim().slice(0, 3000),
    });
  }
  return out;
}
"""

_LINK_ROWS_JS = r"""
(pattern) => {
  const re = new RegExp(pattern);
  const abs = h => new URL(h, location.origin).href.split('?')[0];
  const seen = new Set(), out = [];
  const heads = [...document.querySelectorAll('h2, h3')];
  // Nearest heading that precedes the link in document order (e.g. "Active proposals (2)").
  const sectionOf = el => {
    let h = null;
    for (const x of heads) {
      if (x.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) h = x; else break;
    }
    return h?.innerText?.trim().replace(/\s+/g, ' ') || null;
  };
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href');
    if (!re.test(href)) continue;
    const url = abs(href);
    if (seen.has(url)) continue;
    seen.add(url);
    const row = a.closest('li, tr, article, [role="listitem"], [data-test*="row" i], [data-test*="item" i]') || a;
    const section = sectionOf(a);
    out.push({ url, title: a.innerText.trim().split('\n')[0] || null,
               row_text: row.innerText.trim().slice(0, 2000), section });
  }
  return out;
}
"""

_ROOMS_JS = r"""
() => [...document.querySelectorAll('a[data-test="room-list-item"], [data-test="room-list-item"]')]
  .map(el => {
    const a = el.matches('a') ? el : el.closest('a') || el.querySelector('a');
    const href = a?.getAttribute('href');
    if (!href || !/room_/.test(href)) return null;
    const t = s => el.querySelector(s)?.innerText?.trim() || null;
    return { url: new URL(href, location.origin).href.split('?')[0],
             title: t('[data-test="room-name"]'),
             subtitle: t('[data-test="room-topic-or-subtitle"]'),
             preview: el.innerText.trim().replace(/\s+/g, ' ').slice(0, 1000) };
  }).filter(Boolean)
"""

_STORIES_JS = r"""
() => [...document.querySelectorAll('[data-test="story-container"]')].map(s => ({
  header: s.querySelector('[data-test="story-header"]')?.innerText?.trim().replace(/\s+/g, ' ') || null,
  text: s.querySelector('[data-test="story-message"]')?.innerText?.trim()
        || s.innerText.trim(),
}))
"""

_MAIN_TEXT_JS = r"""
() => (document.querySelector('main') || document.body).innerText
"""


def job_id(url: str) -> str:
    m = re.search(r"~0[0-9a-z]+", url, re.I)
    return m.group(0) if m else hashlib.sha1(url.encode()).hexdigest()[:20]


def last_segment_id(url: str) -> str:
    return url.rstrip("/").split("/")[-1] or hashlib.sha1(url.encode()).hexdigest()[:20]


class NotLoggedIn(RuntimeError):
    pass


class Scraper:
    def __init__(self) -> None:
        self._pw = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(settings.cdp_url)

    async def close(self) -> None:
        # Never close the user's browser; just detach.
        if self._page and not self._page.is_closed():
            await self._page.close()
        if self._pw:
            await self._pw.stop()

    async def _tab(self) -> Page:
        if self._browser is None or not self._browser.is_connected():
            await self.connect()
        if self._page is None or self._page.is_closed():
            ctx = self._browser.contexts[0]
            self._page = await ctx.new_page()
        return self._page

    async def goto(self, url: str) -> Page:
        page = await self._tab()
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await page.wait_for_timeout(random.uniform(1500, 3500))  # human-ish pacing
        if "/account-security/login" in page.url or "/ab/account-security" in page.url:
            raise NotLoggedIn("Upwork is not logged in in the attached Chrome")
        if "challenge" in page.url or await page.title() == "Just a moment...":
            raise RuntimeError("Cloudflare challenge shown; solve it in Chrome and retry")
        return page

    async def is_logged_in(self) -> bool:
        async with self._lock:
            try:
                await self.goto(FEEDS["best_matches"])
                return True
            except NotLoggedIn:
                return False

    # ---- jobs -------------------------------------------------------------

    async def _job_list(self, url: str, source: str) -> list[dict]:
        page = await self.goto(url)
        tiles = await page.evaluate(_JOB_TILES_JS)
        for t in tiles:
            t["id"] = job_id(t["url"])
            t["source"] = source
        return tiles

    async def feed(self, name: str) -> list[dict]:
        if name not in FEEDS:
            raise ValueError(f"feed must be one of {list(FEEDS)}")
        async with self._lock:
            return await self._job_list(FEEDS[name], name)

    async def search_jobs(self, query: str, sort: str = "recency", page: int = 1) -> list[dict]:
        url = f"{BASE}/nx/search/jobs/?q={quote_plus(query)}&sort={sort}&page={page}"
        async with self._lock:
            return await self._job_list(url, f"search:{query}")

    async def job_details(self, url: str) -> dict:
        async with self._lock:
            page = await self.goto(url)
            text = await page.evaluate(_MAIN_TEXT_JS)
            title = (await page.title()).replace(" - Upwork", "").strip()
            return {"id": job_id(url), "url": url, "title": title, "details": text[:50_000]}

    # ---- messages ---------------------------------------------------------

    async def threads(self) -> list[dict]:
        async with self._lock:
            page = await self.goto(MESSAGES_URL)
            rows = await page.evaluate(_ROOMS_JS)
            seen, out = set(), []
            for r in rows:
                if r["url"] in seen:
                    continue
                seen.add(r["url"])
                title = r["title"] or ""
                if r.get("subtitle"):
                    title = f"{title} — {r['subtitle']}"
                out.append({"id": last_segment_id(r["url"]), "url": r["url"],
                            "title": title or None, "preview": r["preview"]})
            return out

    async def read_thread(self, url: str) -> dict:
        async with self._lock:
            page = await self.goto(url)
            stories = await page.evaluate(_STORIES_JS)
            if stories:
                text = "\n\n".join(
                    f"[{s['header']}]\n{s['text']}" if s["header"] else s["text"] for s in stories
                )
            else:
                text = await page.evaluate(_MAIN_TEXT_JS)
            title = await page.evaluate(
                "() => document.querySelector('[data-test=\"room-header-title\"], "
                "[data-test=\"room-title\"]')?.innerText || null"
            )
            return {"id": last_segment_id(url), "url": url, "thread_text": text[:100_000],
                    **({"title": title.strip()} if title else {})}

    async def send_message(self, url: str, text: str) -> dict:
        async with self._lock:
            page = await self.goto(url)
            box = page.locator('[contenteditable="true"]').last
            await box.click()
            await box.type(text, delay=random.uniform(20, 60))
            send = page.locator('[data-test="send-message-button"]')
            if await send.count():
                await send.last.click()
            else:
                await box.press("Enter")
            await page.wait_for_timeout(2500)
            return {"sent": True, "url": url}

    # ---- proposals --------------------------------------------------------

    async def proposals(self) -> list[dict]:
        async with self._lock:
            page = await self.goto(PROPOSALS_URL)
            rows = await page.evaluate(
                _LINK_ROWS_JS, r"/proposals/(?!archived|active|offers|invites)[^/?#]{10,}"
            )
            return [
                {"id": last_segment_id(r["url"]), "url": r["url"], "title": r["title"],
                 "section": r["section"], "row_text": r["row_text"]}
                for r in rows
            ]

    # ---- generic ----------------------------------------------------------

    async def page_text(self, url: str) -> dict:
        if not url.startswith(BASE):
            raise ValueError("Only upwork.com URLs are allowed")
        async with self._lock:
            page = await self.goto(url)
            return {"url": page.url, "title": await page.title(),
                    "text": (await page.evaluate(_MAIN_TEXT_JS))[:50_000]}
