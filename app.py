"""Crawl4AI Hub: one shared crawler service that every agent on this machine can call.

Why a hub instead of "each agent runs crwl":
    every AsyncWebCrawler owns a Chromium process (~300-400 MB, ~2 s cold start).
    Five agents each spawning their own browser is the difference between one
    shared pool and five idle browsers. The hub keeps one browser, one cache and
    one concurrency limit behind a stable localhost contract.

Routes (all JSON, no LLM key required):
    GET  /health              liveness + counters
    GET  /v1/stats            counters only
    POST /v1/crawl            {url, mode, wait_for, js, max_chars, use_cache, remove_overlays}
    POST /v1/search           {query, engine, limit}
    POST /v1/extract          {url, schema}
    POST /v1/screenshot       {url, full_page, out_path}

Run it:
    python app.py            # foreground, Ctrl+C to stop
    python manage.py start   # background, writes a pid file
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import re
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, quote_plus, urlparse

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy

def _load_dotenv() -> None:
    """Read .env next to this file; explicit environment variables win."""
    path = Path(__file__).with_name(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

HUB_VERSION = "0.1.0"
HOST = os.environ.get("C4AI_HUB_HOST", "127.0.0.1")
PORT = int(os.environ.get("C4AI_HUB_PORT", "11235"))
TOKEN = os.environ.get("C4AI_HUB_TOKEN", "")
MAX_CONCURRENCY = int(os.environ.get("C4AI_MAX_CONCURRENCY", "2"))
PAGE_TIMEOUT_MS = int(os.environ.get("C4AI_PAGE_TIMEOUT_MS", "60000"))
CALL_TIMEOUT_S = int(os.environ.get("C4AI_CALL_TIMEOUT_S", "180"))
CACHE_TTL_S = int(os.environ.get("C4AI_CACHE_TTL_S", "900"))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SCHEMAS: dict[str, dict] = {
    "bing": {
        "name": "bing_serp",
        "baseSelector": "li.b_algo",
        "fields": [
            {"name": "title", "selector": "h2", "type": "text"},
            {"name": "url", "selector": "h2 a", "type": "attribute", "attribute": "href"},
            {"name": "snippet", "selector": ".b_caption p, p", "type": "html"},
        ],
    },
    "ddg": {
        "name": "ddg_serp",
        "baseSelector": "div.result, div.web-result",
        "fields": [
            {"name": "title", "selector": ".result__a, h2", "type": "text"},
            {"name": "url", "selector": ".result__a, h2 a", "type": "attribute", "attribute": "href"},
            {"name": "snippet", "selector": ".result__snippet", "type": "html"},
        ],
    },
}

ENGINE_URLS = {
    "ddg": "https://html.duckduckgo.com/html/?q={q}",
    "bing": "https://www.bing.com/search?q={q}&count={n}",
}

# Engines differ in quality on this network; retry the next one when a SERP
# parses to zero results instead of handing the caller an empty list.
FALLBACK_CHAIN = {"bing": ["ddg"], "ddg": []}


def data_dir() -> Path:
    root = Path(
        os.environ.get("C4AI_STATE_DIR")
        or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "crawl4ai-hub")
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _html_to_text(fragment: str | None) -> str:
    if not fragment:
        return ""
    return _clean(html.unescape(re.sub(r"<[^>]+>", " ", fragment)))


def _normalize_url(url: str) -> str:
    if not url:
        return url
    if url.startswith("//"):
        url = "https:" + url
    if "duckduckgo.com/l/" in url:
        target = parse_qs(urlparse(url).query).get("uddg", [""])[0]
        if target:
            return target
    return _decode_bing_redirect(url)


def _decode_bing_redirect(url: str) -> str:
    if "bing.com/ck/a" not in url:
        return url
    raw = parse_qs(urlparse(url).query).get("u", [""])[0]
    if raw.startswith("a1"):
        raw = raw[2:]
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", "ignore")
        return decoded if decoded.startswith("http") else url
    except Exception:  # noqa: BLE001 - a tracking URL is still better than nothing
        return url


class CrawlEngine:
    """One browser, one semaphore, one TTL cache, many callers."""

    def __init__(self) -> None:
        self._crawler: AsyncWebCrawler | None = None
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self._cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._cache_lock = asyncio.Lock()
        self.started_at: float | None = None
        self.browser_error: str | None = None
        self.stats: dict[str, int] = {
            "crawl": 0,
            "search": 0,
            "extract": 0,
            "screenshot": 0,
            "cache_hits": 0,
            "errors": 0,
        }

    # -- lifecycle ---------------------------------------------------------
    def _browser_channel(self) -> str:
        """Prefer Deploy shared strategy: system Chrome/Edge channel, else chromium."""
        for key in ("CRAWL4AI_CHROME_CHANNEL", "CIZHENYU_BROWSER_CHANNEL"):
            v = (os.environ.get(key) or "").strip().lower()
            if v in ("chrome", "msedge", "chromium"):
                return v
        return "chromium"

    async def _launch_browser(self) -> None:
        channel = self._browser_channel()
        browser = BrowserConfig(
            headless=True,
            verbose=False,
            user_agent=UA,
            viewport_width=1366,
            viewport_height=900,
            text_mode=False,
            chrome_channel=channel,
            channel=channel,
        )
        crawler = AsyncWebCrawler(config=browser)
        await crawler.start()
        self._crawler = crawler
        self.browser_error = None

    async def start(self) -> None:
        """Bring hub HTTP up even if Chromium is missing; browser can recover later."""
        self.started_at = time.time()
        try:
            await self._launch_browser()
        except Exception as exc:  # noqa: BLE001
            self.browser_error = str(exc)
            self._crawler = None
            hint = (
                "浏览器未就绪。请安装 Google Chrome / Microsoft Edge，"
                "或由 Deploy 下载工作目录共享 Chromium（browsers/pw）后重试。"
            )
            if "Executable doesn't exist" in self.browser_error or "playwright" in self.browser_error.lower():
                self.browser_error = f"{self.browser_error}\n{hint}"

    async def ensure_browser(self) -> None:
        if self._crawler is not None:
            return
        try:
            await self._launch_browser()
        except Exception as exc:  # noqa: BLE001
            self.browser_error = str(exc)
            raise RuntimeError(
                f"浏览器未启动：{exc}。请安装 Chrome/Edge，或使用 Deploy 共享 Chromium。"
            ) from exc

    async def close(self) -> None:
        if self._crawler is not None:
            await self._crawler.close()
            self._crawler = None

    @property
    def ready(self) -> bool:
        # Hub process is up once start() ran; browser may still be recovering.
        return self.started_at is not None

    @property
    def browser_ready(self) -> bool:
        return self._crawler is not None

    # -- cache -------------------------------------------------------------
    async def _cache_get(self, key: str) -> dict | None:
        async with self._cache_lock:
            hit = self._cache.get(key)
            if not hit:
                return None
            stamp, payload = hit
            if time.time() - stamp > CACHE_TTL_S:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            self.stats["cache_hits"] += 1
            return payload

    async def _cache_put(self, key: str, payload: dict) -> None:
        async with self._cache_lock:
            self._cache[key] = (time.time(), payload)
            self._cache.move_to_end(key)
            while len(self._cache) > 256:
                self._cache.popitem(last=False)

    # -- operations --------------------------------------------------------
    async def crawl(
        self,
        url: str,
        mode: Literal["markdown", "html", "both"] = "markdown",
        wait_for: str | None = None,
        js: str | None = None,
        max_chars: int = 20000,
        use_cache: bool = True,
        remove_overlays: bool = True,
    ) -> dict:
        self._require_url(url)
        key = json.dumps(["crawl", url, mode, wait_for, js, max_chars, remove_overlays], sort_keys=True)
        if use_cache:
            hit = await self._cache_get(key)
            if hit is not None:
                return {**hit, "cached": True, "elapsed_ms": 0}

        cfg = CrawlerRunConfig(
            cache_mode=CacheMode.ENABLED if use_cache else CacheMode.BYPASS,
            page_timeout=PAGE_TIMEOUT_MS,
            wait_for=wait_for,
            js_code=js,
            remove_overlay_elements=remove_overlays,
            delay_before_return_html=0.4,
            verbose=False,
        )
        started = time.perf_counter()
        async with self._sem:
            result = await asyncio.wait_for(self._call(url, cfg), timeout=CALL_TIMEOUT_S)
        if not result.success:
            raise RuntimeError(result.error_message or "crawl failed")

        markdown = ""
        if mode in ("markdown", "both"):
            md = getattr(result, "markdown", None)
            markdown = getattr(md, "raw_markdown", None) or (str(md) if md else "")
            markdown = markdown[:max_chars]
        body = ""
        if mode in ("html", "both"):
            body = (result.html or "")[: max_chars * 4]

        links = result.links or {}
        external = [l.get("href") for l in links.get("external", []) if isinstance(l, dict)][:20]
        payload = {
            "ok": True,
            "url": url,
            "final_url": getattr(result, "url", url),
            "status": getattr(result, "status_code", None),
            "title": (getattr(result, "metadata", None) or {}).get("title"),
            "markdown": markdown,
            "html": body,
            "links": {
                "internal": len(links.get("internal", [])),
                "external": len(links.get("external", [])),
                "external_sample": external,
            },
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "cached": False,
        }
        if use_cache:
            await self._cache_put(key, payload)
        return payload

    async def search(self, query: str, engine: str = "ddg", limit: int = 10) -> dict:
        engine = engine if engine in ENGINE_URLS else "ddg"
        tried: list[dict] = []
        for candidate in [engine, *FALLBACK_CHAIN.get(engine, [])]:
            attempt = await self._search_once(candidate, query, limit)
            tried.append({"engine": candidate, "kept": len(attempt["results"])})
            if attempt["results"]:
                attempt["attempts"] = tried
                return attempt
        return {
            "ok": True,
            "query": query,
            "engine": engine,
            "results": [],
            "attempts": tried,
            "note": "no parsed results; the SERP may be a consent/captcha page",
        }

    async def _search_once(self, engine: str, query: str, limit: int) -> dict:
        url = ENGINE_URLS[engine].format(q=quote_plus(query), n=limit)
        cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            extraction_strategy=JsonCssExtractionStrategy(schema=SCHEMAS[engine], verbose=False),
            page_timeout=PAGE_TIMEOUT_MS,
            delay_before_return_html=1.2,
            verbose=False,
        )
        started = time.perf_counter()
        async with self._sem:
            result = await asyncio.wait_for(self._call(url, cfg), timeout=CALL_TIMEOUT_S)
        if not result.success:
            raise RuntimeError(result.error_message or "search failed")

        rows = json.loads(result.extracted_content or "[]")
        cleaned: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            link = _normalize_url(_clean(row.get("url")))
            title = _clean(row.get("title"))
            if not title or not link or link in seen:
                continue
            seen.add(link)
            cleaned.append({"title": title, "url": link, "snippet": _html_to_text(row.get("snippet"))})
            if len(cleaned) >= limit:
                break
        return {
            "ok": True,
            "query": query,
            "engine": engine,
            "serp_url": url,
            "results": cleaned,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    async def extract(self, url: str, schema: dict) -> dict:
        self._require_url(url)
        cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            extraction_strategy=JsonCssExtractionStrategy(schema=schema, verbose=False),
            page_timeout=PAGE_TIMEOUT_MS,
            verbose=False,
        )
        started = time.perf_counter()
        async with self._sem:
            result = await asyncio.wait_for(self._call(url, cfg), timeout=CALL_TIMEOUT_S)
        if not result.success:
            raise RuntimeError(result.error_message or "extract failed")
        return {
            "ok": True,
            "url": url,
            "final_url": getattr(result, "url", url),
            "data": json.loads(result.extracted_content or "[]"),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    async def screenshot(self, url: str, full_page: bool = False, out_path: str | None = None) -> dict:
        self._require_url(url)
        cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            screenshot=True,
            page_timeout=PAGE_TIMEOUT_MS,
            remove_overlay_elements=True,
            verbose=False,
        )
        started = time.perf_counter()
        async with self._sem:
            result = await asyncio.wait_for(self._call(url, cfg), timeout=CALL_TIMEOUT_S)
        if not result.success:
            raise RuntimeError(result.error_message or "screenshot failed")
        raw = result.screenshot or ""
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[-1]
        target = Path(out_path) if out_path else data_dir() / "screenshots" / f"{uuid.uuid4().hex}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(raw))
        return {
            "ok": True,
            "url": url,
            "path": str(target),
            "bytes": target.stat().st_size,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    # -- helpers -----------------------------------------------------------
    async def _call(self, url: str, cfg: CrawlerRunConfig):
        await self.ensure_browser()
        assert self._crawler is not None
        return await self._crawler.arun(url=url, config=cfg)

    @staticmethod
    def _require_url(url: str) -> None:
        if not re.match(r"^https?://", url or "", re.I):
            raise ValueError("url must be an absolute http(s) URL")


engine = CrawlEngine()


def _auth(x_c4ai_token: str | None = Header(default=None, alias="X-C4AI-Token")) -> None:
    if TOKEN and x_c4ai_token != TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-C4AI-Token")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await engine.start()
    try:
        yield
    finally:
        await engine.close()


app = FastAPI(
    title="Crawl4AI Hub",
    version=HUB_VERSION,
    description="Shared Playwright/Crawl4AI service for local agents",
    lifespan=lifespan,
)


class CrawlRequest(BaseModel):
    url: str = Field(description="absolute http(s) URL")
    mode: Literal["markdown", "html", "both"] = "markdown"
    wait_for: str | None = Field(default=None, description="CSS selector or 'css:...' to wait for")
    js: str | None = Field(default=None, description="JavaScript to run before capture")
    max_chars: int = Field(default=20000, ge=500, le=400000)
    use_cache: bool = True
    remove_overlays: bool = True


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    engine: Literal["ddg", "bing"] = "ddg"
    limit: int = Field(default=10, ge=1, le=30)


class ExtractRequest(BaseModel):
    url: str
    schema: dict


class ScreenshotRequest(BaseModel):
    url: str
    full_page: bool = False
    out_path: str | None = None


def _fail(exc: Exception) -> HTTPException:
    engine.stats["errors"] += 1
    status = 400 if isinstance(exc, ValueError) else 504 if isinstance(exc, asyncio.TimeoutError) else 502
    return HTTPException(status_code=status, detail=f"{type(exc).__name__}: {exc}")


@app.get("/")
def root() -> dict:
    return {
        "service": "crawl4ai-hub",
        "version": HUB_VERSION,
        "endpoints": ["/health", "/v1/stats", "/v1/crawl", "/v1/search", "/v1/extract", "/v1/screenshot"],
        "docs": "/docs",
    }


@app.get("/health")
def health() -> dict:
    return {
        "ok": engine.ready,
        "browser": engine.browser_ready,
        "browser_error": engine.browser_error,
        "version": HUB_VERSION,
        "uptime_s": int(time.time() - engine.started_at) if engine.started_at else 0,
        "concurrency": MAX_CONCURRENCY,
        "stats": engine.stats,
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@app.get("/v1/stats", dependencies=[Depends(_auth)])
def stats() -> dict:
    return {"ok": True, "stats": engine.stats, "cache_entries": len(engine._cache)}


@app.post("/v1/crawl", dependencies=[Depends(_auth)])
async def crawl(req: CrawlRequest) -> dict:
    engine.stats["crawl"] += 1
    try:
        return await engine.crawl(
            req.url,
            mode=req.mode,
            wait_for=req.wait_for,
            js=req.js,
            max_chars=req.max_chars,
            use_cache=req.use_cache,
            remove_overlays=req.remove_overlays,
        )
    except Exception as exc:  # noqa: BLE001 - mapped to an HTTP status
        raise _fail(exc) from exc


@app.post("/v1/search", dependencies=[Depends(_auth)])
async def search(req: SearchRequest) -> dict:
    engine.stats["search"] += 1
    try:
        return await engine.search(req.query, engine=req.engine, limit=req.limit)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@app.post("/v1/extract", dependencies=[Depends(_auth)])
async def extract(req: ExtractRequest) -> dict:
    engine.stats["extract"] += 1
    try:
        return await engine.extract(req.url, req.schema)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@app.post("/v1/screenshot", dependencies=[Depends(_auth)])
async def screenshot(req: ScreenshotRequest) -> dict:
    engine.stats["screenshot"] += 1
    try:
        return await engine.screenshot(req.url, full_page=req.full_page, out_path=req.out_path)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level=os.environ.get("C4AI_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
