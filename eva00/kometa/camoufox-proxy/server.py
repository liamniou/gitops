"""Minimal FlareSolverr-compatible fetcher backed by a Camoufox (Firefox) browser.

POST /v1  {"cmd": "request.get", "url": "...", "maxTimeout": 60000}
GET  /health

Requests are served one at a time by a single browser that is launched lazily
and closed after IDLE_TIMEOUT seconds without requests. If a Cloudflare
challenge is not passed within CHALLENGE_TIMEOUT, the browser is relaunched
(fresh fingerprint) and the request retried.
"""

import asyncio
import logging
import os
import time
from urllib.parse import urlparse

from aiohttp import web
from camoufox.async_api import AsyncCamoufox

PORT = int(os.environ.get("PORT", "8191"))
ALLOWED_DOMAINS = [d.strip().lower() for d in os.environ.get("ALLOWED_DOMAINS", "letterboxd.com").split(",") if d.strip()]
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "300"))
RECYCLE_AFTER = int(os.environ.get("RECYCLE_AFTER", "500"))
CHALLENGE_TIMEOUT = int(os.environ.get("CHALLENGE_TIMEOUT", "30"))
DEFAULT_TIMEOUT_MS = int(os.environ.get("DEFAULT_TIMEOUT_MS", "90000"))
BLOCK_RESOURCES = {"image", "media", "font"}
CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser", "please wait")
TURNSTILE_CLICK_AFTER = 8
VERSION = "1.1.0"

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("camoufox-proxy")


class ChallengeTimeout(Exception):
    pass


def is_challenge(title):
    title = (title or "").lower()
    return any(t in title for t in CHALLENGE_TITLES)


class Browser:
    def __init__(self):
        self._cm = None
        self._context = None
        self._lock = asyncio.Lock()
        self._served = 0
        self.last_used = 0.0

    @property
    def running(self):
        return self._context is not None

    async def _launch(self):
        log.info("Launching Camoufox")
        # disable_coop allows clicking the cross-origin Turnstile checkbox.
        self._cm = AsyncCamoufox(headless=True, locale="en-US", block_webrtc=True, disable_coop=True, i_know_what_im_doing=True)
        browser = await self._cm.__aenter__()
        # One shared context keeps cf_clearance cookies between requests.
        self._context = await browser.new_context()
        await self._context.route("**/*", self._route)
        self._served = 0

    @staticmethod
    async def _route(route):
        if route.request.resource_type in BLOCK_RESOURCES:
            await route.abort()
        else:
            await route.continue_()

    async def _close(self):
        cm, self._cm, self._context = self._cm, None, None
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception as e:
                log.warning("Error closing browser: %s", e)

    async def close(self):
        async with self._lock:
            await self._close()

    async def close_if_idle(self):
        if self._lock.locked():
            return
        async with self._lock:
            if self._context is not None and time.monotonic() - self.last_used > IDLE_TIMEOUT:
                log.info("Closing idle browser")
                await self._close()

    async def fetch(self, url, timeout_ms):
        deadline = time.monotonic() + timeout_ms / 1000
        async with self._lock:
            try:
                for attempt in (1, 2):
                    if self._context is not None and self._served >= RECYCLE_AFTER:
                        log.info("Recycling browser after %d requests", self._served)
                        await self._close()
                    if self._context is None:
                        await self._launch()
                    self._served += 1
                    remaining = deadline - time.monotonic()
                    try:
                        return await self._fetch(url, deadline, min(CHALLENGE_TIMEOUT, remaining))
                    except ChallengeTimeout:
                        await self._close()
                        if attempt == 2 or deadline - time.monotonic() < 10:
                            raise
                        log.info("Challenge not passed for %s; relaunching browser and retrying", url)
                    except Exception:
                        # A crashed browser is relaunched on the next request.
                        await self._close()
                        raise
            finally:
                self.last_used = time.monotonic()

    async def _fetch(self, url, deadline, challenge_timeout):
        page = await self._context.new_page()
        nav = {}

        def on_response(resp):
            if resp.request.is_navigation_request() and resp.frame == page.main_frame:
                nav["resp"] = resp

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=max(1000, int((deadline - time.monotonic()) * 1000)))
            started, clicked, solved = time.monotonic(), False, False
            while is_challenge(await page.title()):
                solved = True
                waited = time.monotonic() - started
                if waited > challenge_timeout:
                    raise ChallengeTimeout(f"Cloudflare challenge not passed within {int(waited)}s")
                if not clicked and waited > TURNSTILE_CLICK_AFTER:
                    clicked = await self._click_turnstile(page)
                await page.wait_for_timeout(1000)
            if solved:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
            resp = nav.get("resp")
            headers = await resp.all_headers() if resp else {}
            # Drop encoding/length headers: the body returned is decoded HTML.
            headers = {k: v for k, v in headers.items() if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")}
            # Prefer the raw server HTML (what a plain HTTP client would get) over the JS-mutated DOM.
            try:
                body = await resp.text() if resp else await page.content()
            except Exception:
                body = await page.content()
            return {
                "url": page.url,
                "status": resp.status if resp else 200,
                "headers": headers,
                "response": body,
                "cookies": await self._context.cookies(),
                "userAgent": await page.evaluate("navigator.userAgent"),
            }, solved
        finally:
            await page.close()

    @staticmethod
    async def _click_turnstile(page):
        """Best-effort click on the Turnstile checkbox (left side of the widget)."""
        try:
            for frame in page.frames:
                if "challenges.cloudflare.com" in frame.url:
                    element = await frame.frame_element()
                    box = await element.bounding_box()
                    if box and box["width"] > 10:
                        await page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                        log.info("Clicked Turnstile checkbox")
                        return True
            widget = await page.query_selector("input[name='cf-turnstile-response']")
            if widget:
                box = await (await widget.evaluate_handle("e => e.parentElement")).as_element().bounding_box()
                if box and box["width"] > 10:
                    await page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                    log.info("Clicked Turnstile widget")
                    return True
        except Exception as e:
            log.debug("Turnstile click failed: %s", e)
        return False


browser = Browser()


def allowed(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith(f".{d}") for d in ALLOWED_DOMAINS)


def error(message, status, start):
    now = int(time.time() * 1000)
    return web.json_response({"status": "error", "message": message, "startTimestamp": start, "endTimestamp": now, "version": VERSION}, status=status)


async def handle_v1(request):
    start = int(time.time() * 1000)
    try:
        body = await request.json()
    except Exception:
        return error("Invalid JSON body", 400, start)
    cmd = body.get("cmd")
    url = body.get("url") or ""
    if cmd != "request.get":
        return error(f"Unsupported cmd: {cmd}", 400, start)
    if urlparse(url).scheme not in ("http", "https") or not allowed(url):
        return error(f"URL not allowed: {url}", 403, start)
    timeout_ms = int(body.get("maxTimeout") or DEFAULT_TIMEOUT_MS)
    try:
        solution, solved = await browser.fetch(url, timeout_ms)
    except Exception as e:
        log.warning("Fetch failed for %s: %s", url, e)
        return error(f"Error solving the challenge. {e}", 500, start)
    log.info("%s -> %s%s (%d ms)", url, solution["status"], " [challenge passed]" if solved else "", int(time.time() * 1000) - start)
    return web.json_response({
        "status": "ok",
        "message": "Challenge solved!" if solved else "Challenge not detected!",
        "solution": solution,
        "startTimestamp": start,
        "endTimestamp": int(time.time() * 1000),
        "version": VERSION,
    })


async def handle_health(_request):
    return web.json_response({"status": "ok", "browser_running": browser.running, "version": VERSION})


async def idle_reaper(_app):
    async def loop():
        while True:
            await asyncio.sleep(30)
            await browser.close_if_idle()

    task = asyncio.create_task(loop())
    yield
    task.cancel()
    await browser.close()


def main():
    app = web.Application(client_max_size=1024 * 1024)
    app.add_routes([web.post("/v1", handle_v1), web.get("/health", handle_health)])
    app.cleanup_ctx.append(idle_reaper)
    log.info("Listening on :%d, allowed domains: %s", PORT, ", ".join(ALLOWED_DOMAINS))
    web.run_app(app, port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
