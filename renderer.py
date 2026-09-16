import asyncio
import hashlib
import json
import logging
import re
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
from playwright.async_api import async_playwright

ORIGIN = "https://satsuiteeducatorquestionbank.collegeboard.org"
PAGE_URL = f"{ORIGIN}/sat-bot-render"
ALLOWED_HOSTS = {"satsuiteeducatorquestionbank.collegeboard.org", "atlas.collegeboard.org", "cdn.jsdelivr.net"}
FALLBACK_STYLESHEETS = [
    "https://atlas.collegeboard.org/apricot/prod/4.5.12/illustrations.css",
    "https://atlas.collegeboard.org/apricot/prod/4.5.19/robotoslab.css",
    f"{ORIGIN}/assets/index-DVZ6-BOy.css",
]
TEMPLATE = Path(__file__).with_name("template.html")
SKIPPED_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection"}

log = logging.getLogger("renderer")


async def discover_stylesheets():
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(f"{ORIGIN}/") as response:
                response.raise_for_status()
                html = await response.text()
    except Exception as error:
        log.warning("Could not load the question bank page, using known stylesheets: %s", error)
        return FALLBACK_STYLESHEETS
    links = []
    for tag in re.findall(r"<link\b[^>]*>", html, flags=re.IGNORECASE):
        if re.search(r"rel=[\"']?stylesheet", tag, flags=re.IGNORECASE):
            href = re.search(r"href=[\"']([^\"']+)[\"']", tag)
            if href:
                links.append(urljoin(f"{ORIGIN}/", href.group(1)))
    return links or FALLBACK_STYLESHEETS


class Renderer:
    def __init__(self, cache_dir="data/cache", pages=2, scale=2.0, quality=90, memory_items=64):
        self.cache_dir = Path(cache_dir)
        self.page_count = pages
        self.scale = scale
        self.quality = quality
        self.memory_items = memory_items
        self.images = OrderedDict()
        self.pages = asyncio.Queue()
        self.playwright = None
        self.browser = None
        self.context = None
        self.html = None

    async def start(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        stylesheets = await discover_stylesheets()
        links = "\n".join(f'<link rel="stylesheet" crossorigin href="{href}">' for href in stylesheets)
        self.html = TEMPLATE.read_text(encoding="utf-8").replace("__STYLESHEETS__", links)
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(args=["--disable-dev-shm-usage", "--no-sandbox"])
        self.context = await self.browser.new_context(
            viewport={"width": 1400, "height": 1000}, device_scale_factor=self.scale, locale="en-US"
        )
        await self.context.route("**/*", self._route)
        for _ in range(self.page_count):
            await self.pages.put(await self._new_page())
        log.info("Renderer ready with %s page(s)", self.page_count)

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def _new_page(self):
        page = await self.context.new_page()
        await page.goto(PAGE_URL, wait_until="load")
        await page.wait_for_function("window.__MathJax_State__ && window.__MathJax_State__.isReady", timeout=60000)
        return page

    async def _replace(self, page):
        try:
            await page.close()
        except Exception:
            pass
        try:
            return await self._new_page()
        except Exception:
            log.exception("Could not reopen a render page")
            return page

    async def _route(self, route):
        request = route.request
        url = request.url
        if url == PAGE_URL:
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=self.html)
            return
        if urlparse(url).hostname not in ALLOWED_HOSTS or request.method != "GET":
            await route.abort()
            return
        key = hashlib.sha256(url.encode()).hexdigest()
        body_path = self.cache_dir / key
        headers_path = self.cache_dir / f"{key}.json"
        if body_path.exists() and headers_path.exists():
            headers = json.loads(headers_path.read_text())
            await route.fulfill(status=200, headers=headers, body=body_path.read_bytes())
            return
        try:
            response = await route.fetch()
        except Exception:
            await route.abort()
            return
        body = await response.body()
        headers = {name: value for name, value in response.headers.items() if name.lower() not in SKIPPED_HEADERS}
        if response.ok:
            body_path.write_bytes(body)
            headers_path.write_text(json.dumps(headers))
        await route.fulfill(status=response.status, headers=headers, body=body)

    async def render(self, question, reveal=False):
        key = (question.question_id, reveal, question.updated_at)
        if key in self.images:
            self.images.move_to_end(key)
            return self.images[key]
        payload = {
            "questionId": question.question_id,
            "assessment": "SAT",
            "section": question.section,
            "domain": question.domain,
            "skill": question.skill,
            "difficulty": question.difficulty,
            "legacy": question.legacy,
            "content": question.content,
            "reveal": reveal,
        }
        page = await self.pages.get()
        try:
            await page.evaluate("window.renderQuestion", payload)
            image = await page.locator(".cb-modal-container").screenshot(
                type="jpeg", quality=self.quality, animations="disabled", timeout=60000
            )
        except Exception:
            page = await self._replace(page)
            raise
        finally:
            await self.pages.put(page)
        self.images[key] = image
        while len(self.images) > self.memory_items:
            self.images.popitem(last=False)
        return image
