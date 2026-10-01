"""Kometa shim: route Cloudflare-blocked Letterboxd fetches through camoufox-proxy.

Loaded automatically via PYTHONPATH=/shim. Patches letterboxdpy's Scraper._fetch,
which both letterboxdpy and Kometa's Letterboxd fallback path use.

Env:
  CAMOUFOX_PROXY_URL     e.g. http://camoufox-proxy:8191 (unset = shim disabled)
  LETTERBOXD_PROXY_MODE  fallback (direct first, proxy on block) | always | off
"""

import importlib.abc
import importlib.util
import json
import logging
import os
import sys
import urllib.request

PROXY_URL = os.environ.get("CAMOUFOX_PROXY_URL", "").rstrip("/")
MODE = os.environ.get("LETTERBOXD_PROXY_MODE", "fallback").strip().lower()
TIMEOUT_MS = int(os.environ.get("LETTERBOXD_PROXY_TIMEOUT_MS", "90000"))
TARGET = "letterboxdpy.core.scraper"
BLOCKED_STATUSES = {403, 429, 503}

log = logging.getLogger("Kometa")


class _ProxyResponse:
    def __init__(self, solution, headers_cls):
        self.url = solution.get("url")
        self.status_code = int(solution.get("status") or 0)
        self.headers = headers_cls(solution.get("headers") or {})
        self.text = solution.get("response") or ""
        self.content = self.text.encode("utf-8")
        self.reason = ""


def _fetch_via_proxy(url, headers_cls):
    payload = json.dumps({"cmd": "request.get", "url": url, "maxTimeout": TIMEOUT_MS}).encode()
    req = urllib.request.Request(f"{PROXY_URL}/v1", data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_MS / 1000 + 15) as resp:
        data = json.load(resp)
    if data.get("status") != "ok":
        raise RuntimeError(data.get("message"))
    return _ProxyResponse(data["solution"], headers_cls)


def _patch(module):
    from curl_cffi.requests import Headers

    scraper = module.Scraper
    original_fetch = scraper._fetch.__func__

    def _fetch(cls, url):
        if MODE != "always":
            try:
                response = cls.instance().get(url, headers=cls.headers, timeout=cls.timeout, impersonate="chrome")
                if response.status_code not in BLOCKED_STATUSES:
                    return response
                log.info(f"Letterboxd: direct request got HTTP {response.status_code} for {url}; retrying via camoufox-proxy")
            except Exception as e:
                log.info(f"Letterboxd: direct request failed for {url} ({e}); retrying via camoufox-proxy")
        try:
            return _fetch_via_proxy(url, Headers)
        except Exception as e:
            log.warning(f"Letterboxd: camoufox-proxy failed for {url} ({e}); falling back to letterboxdpy default fetch")
            return original_fetch(cls, url)

    scraper._fetch = classmethod(_fetch)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != TARGET:
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(fullname)
        if spec is None or spec.loader is None:
            return spec
        exec_module = spec.loader.exec_module

        def patched_exec(module):
            exec_module(module)
            try:
                _patch(module)
            except Exception as e:
                log.warning(f"camoufox shim: failed to patch letterboxdpy ({e})")

        spec.loader.exec_module = patched_exec
        return spec


if PROXY_URL and MODE != "off":
    sys.meta_path.insert(0, _Finder())
