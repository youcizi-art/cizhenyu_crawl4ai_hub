#!/usr/bin/env python3
"""crawl4ai MCP server: a thin stdio bridge to the local Crawl4AI Hub.

Transport: stdio, newline-delimited JSON-RPC 2.0 (MCP 2025-06-18 shape).
No third-party dependencies. The heavy lifting (Chromium, cache, concurrency)
lives in `app.py`, so registering this server in ten agents still costs one
browser, not ten.

Start the hub first:
    python manage.py start

Protocol smoke test without an agent:
    python mcp_server.py
    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

SERVER_NAME = "crawl4ai"
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOLS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-06-18"
MAX_OUTPUT_CHARS = 60_000

def _load_hub_token() -> str:
    """Prefer process env, then Deploy auth dir crawl4ai.json / hub.json. Never hardcode."""
    env = os.environ.get("C4AI_HUB_TOKEN", "").strip()
    if env:
        return env
    for key in ("CIZHENYU_AUTH_DIR", "SEOGEO_AUTH_DIR"):
        base = os.environ.get(key, "").strip()
        if not base:
            continue
        for name in ("crawl4ai.json", "hub.json"):
            path = Path(base) / name
            if path.is_file():
                try:
                    conf = json.loads(path.read_text(encoding="utf-8-sig"))
                    token = (conf.get("hubToken") or conf.get("token") or conf.get("C4AI_HUB_TOKEN") or "").strip()
                    if token:
                        return token
                except (OSError, ValueError, TypeError):
                    pass
    return ""


HUB_URL = os.environ.get("C4AI_HUB_URL", "http://127.0.0.1:11235").rstrip("/")
HUB_TOKEN = _load_hub_token()
HUB_TIMEOUT = float(os.environ.get("C4AI_HUB_TIMEOUT", "180"))

INSTRUCTIONS = (
    "Web crawling backed by a shared local Crawl4AI service. Use crawl_url to turn a "
    "page into Markdown, search_web to get current search results with titles, URLs "
    "and snippets, extract_json for CSS-schema extraction, and screenshot_page for a "
    "rendered PNG. Cite the returned URLs as evidence and keep values exactly as "
    "returned; do not invent numbers that the page did not provide."
)


def _json_schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOLS = [
    {
        "name": "crawl_url",
        "description": (
            "Fetch one URL with a real browser and return its Markdown (or raw HTML). "
            "Use for reading a specific page, docs, or a JS-rendered site."
        ),
        "inputSchema": _json_schema(
            {
                "url": {"type": "string", "description": "Absolute http(s) URL"},
                "mode": {"type": "string", "enum": ["markdown", "html", "both"], "default": "markdown"},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": 200000, "default": 12000},
                "wait_for": {
                    "type": "string",
                    "description": "Optional CSS selector (or 'css:...') to wait for before capture",
                },
            },
            ["url"],
        ),
    },
    {
        "name": "search_web",
        "description": (
            "Run a web search and return ranked results with title, URL and snippet. "
            "Crawls the SERP itself (DuckDuckGo by default, Bing optional)."
        ),
        "inputSchema": _json_schema(
            {
                "query": {"type": "string", "description": "Search query"},
                "engine": {"type": "string", "enum": ["ddg", "bing"], "default": "ddg"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 8},
            },
            ["query"],
        ),
    },
    {
        "name": "extract_json",
        "description": (
            "Extract structured records from a page using a CSS schema "
            "(baseSelector + fields), returning clean JSON."
        ),
        "inputSchema": _json_schema(
            {
                "url": {"type": "string", "description": "Absolute http(s) URL"},
                "schema": {
                    "type": "object",
                    "description": "baseSelector/fields schema, e.g. {'name':'x','baseSelector':'li','fields':[{'name':'t','selector':'h2','type':'text'}]}",
                },
            },
            ["url", "schema"],
        ),
    },
    {
        "name": "screenshot_page",
        "description": "Render a page and save a PNG locally; returns the absolute file path.",
        "inputSchema": _json_schema(
            {
                "url": {"type": "string", "description": "Absolute http(s) URL"},
                "full_page": {"type": "boolean", "default": False},
                "out_path": {"type": "string", "description": "Optional absolute PNG path"},
            },
            ["url"],
        ),
    },
]


class HubUnavailable(RuntimeError):
    pass


def _call_hub(path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if HUB_TOKEN:
        headers["X-C4AI-Token"] = HUB_TOKEN
    request = urllib.request.Request(f"{HUB_URL}{path}", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=HUB_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        raise RuntimeError(f"hub returned {exc.code}: {detail}") from exc
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise HubUnavailable(
            f"crawl4ai hub is not reachable at {HUB_URL} ({exc}). "
            "Start it with: python scripts/c4ai_hub.py start"
        ) from exc


def tool_crawl_url(arguments: dict) -> dict:
    url = arguments.get("url")
    if not url:
        raise ValueError("url is required")
    return _call_hub(
        "/v1/crawl",
        {
            "url": url,
            "mode": arguments.get("mode", "markdown"),
            "max_chars": int(arguments.get("max_chars", 12000)),
            "wait_for": arguments.get("wait_for"),
        },
    )


def tool_search_web(arguments: dict) -> dict:
    query = arguments.get("query")
    if not query:
        raise ValueError("query is required")
    return _call_hub(
        "/v1/search",
        {"query": query, "engine": arguments.get("engine", "ddg"), "limit": int(arguments.get("limit", 8))},
    )


def tool_extract_json(arguments: dict) -> dict:
    url = arguments.get("url")
    schema = arguments.get("schema")
    if not url or not isinstance(schema, dict):
        raise ValueError("url and schema are required")
    return _call_hub("/v1/extract", {"url": url, "schema": schema})


def tool_screenshot_page(arguments: dict) -> dict:
    url = arguments.get("url")
    if not url:
        raise ValueError("url is required")
    return _call_hub(
        "/v1/screenshot",
        {"url": url, "full_page": bool(arguments.get("full_page", False)), "out_path": arguments.get("out_path")},
    )


HANDLERS = {
    "crawl_url": tool_crawl_url,
    "search_web": tool_search_web,
    "extract_json": tool_extract_json,
    "screenshot_page": tool_screenshot_page,
}


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(request_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id, code: int, message: str, data: dict | None = None) -> None:
    payload = {"code": code, "message": message}
    if data:
        payload["data"] = data
    _send({"jsonrpc": "2.0", "id": request_id, "error": payload})


def _truncate(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return json.dumps(
        {
            **{k: v for k, v in payload.items() if k not in ("markdown", "html")},
            "truncated": True,
            "note": f"output exceeded {MAX_OUTPUT_CHARS} chars; markdown/html omitted",
        },
        ensure_ascii=False,
    )


def handle(request: dict) -> None:
    method = request.get("method", "")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        protocol = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        _result(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            },
        )
        return

    if method in ("notifications/initialized", "notifications/cancelled", "initialized"):
        return

    if method == "ping":
        _result(request_id, {})
        return

    if method == "tools/list":
        _result(request_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            _error(request_id, -32602, f"unknown tool: {name}", {"available": sorted(HANDLERS)})
            return
        try:
            payload = handler(arguments)
            _result(request_id, {"content": [{"type": "text", "text": _truncate(payload)}], "isError": False})
        except (ValueError, KeyError, TypeError) as exc:
            _result(
                request_id,
                {"content": [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}], "isError": True},
            )
        except Exception as exc:  # noqa: BLE001 - remote/unexpected failures
            print(f"[{SERVER_NAME}] tool {name} failed: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            _result(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)}
                    ],
                    "isError": True,
                },
            )
        return

    if method == "resources/list":
        _result(request_id, {"resources": []})
        return

    if request_id is None:
        return

    _error(request_id, -32601, f"method not found: {method}")


def main() -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    print(f"[{SERVER_NAME}] ready (stdio -> {HUB_URL})", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError as exc:
            _error(None, -32700, f"parse error: {exc}")
            continue
        if not isinstance(request, dict):
            _error(None, -32600, "request must be a JSON object")
            continue
        try:
            handle(request)
        except Exception as exc:  # pragma: no cover
            print(f"[{SERVER_NAME}] handler crashed: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            if request.get("id") is not None:
                _error(request.get("id"), -32603, f"internal error: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
