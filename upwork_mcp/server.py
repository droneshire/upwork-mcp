"""Upwork MCP server. Talks only to Supabase, so it can run anywhere.

Reads come from stored data. Live actions are queued as `commands` for the
local worker (attached to your logged-in Chrome) and awaited.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from .config import settings
from .db import DB

mcp = MCPServer(
    "upwork",
    instructions=(
        "Access the user's Upwork account. list_* / get_* tools read data stored in the "
        "cloud (synced periodically). refresh_* / live_* tools ask the local Chrome worker "
        "to fetch fresh data and may take up to a couple of minutes. Never send messages "
        "without the user's explicit approval of the exact text."
    ),
)

_db: DB | None = None


def db() -> DB:
    global _db
    if _db is None:
        _db = DB()
    return _db


def _clip(s: str | None, n: int) -> str | None:
    return s if s is None or len(s) <= n else s[:n] + "…"


async def _worker_alive() -> tuple[bool, dict | None]:
    rows = await db().select("worker_status", {"order": "last_heartbeat.desc", "limit": "1"})
    if not rows:
        return False, None
    hb = datetime.fromisoformat(rows[0]["last_heartbeat"])
    age = (datetime.now(timezone.utc) - hb).total_seconds()
    return age < settings.worker_stale_seconds, {**rows[0], "heartbeat_age_s": round(age)}


async def _run(ctype: str, args: dict[str, Any] | None = None) -> Any:
    alive, status = await _worker_alive()
    if not alive:
        raise RuntimeError(
            f"Local Chrome worker is offline (last status: {status}). Start Chrome with "
            "scripts/start-chrome.sh and run `upwork-worker` on that machine."
        )
    cmd = await db().insert("commands", {"type": ctype, "args": args or {}})
    deadline = time.monotonic() + settings.command_timeout
    delay = 1.0
    while time.monotonic() < deadline:
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 5)
        row = (await db().select("commands", {"id": f"eq.{cmd['id']}",
                                              "select": "status,result,error"}))[0]
        if row["status"] == "done":
            return row["result"]
        if row["status"] == "error":
            raise RuntimeError(row["error"])
    raise TimeoutError(f"Command {cmd['id']} ({ctype}) still pending after "
                       f"{settings.command_timeout}s; it will still run, check back later.")


def _job_summary(j: dict) -> dict:
    return {k: j.get(k) for k in ("id", "url", "title", "posted", "budget", "skills",
                                  "meta", "source", "first_seen")} | {
        "description": _clip(j.get("description") or j.get("tile_text"), 400)}


def _id_filter(id_or_url: str) -> dict[str, str]:
    col = "url" if id_or_url.startswith("http") else "id"
    return {col: f"eq.{id_or_url.split('?')[0]}"}


# ---- status ---------------------------------------------------------------

@mcp.tool()
async def worker_status() -> dict:
    """Check whether the local Chrome worker is online and logged into Upwork."""
    alive, status = await _worker_alive()
    return {"online": alive, "status": status}


# ---- jobs -----------------------------------------------------------------

@mcp.tool()
async def list_jobs(
    search: str | None = None,
    since_hours: float | None = 24,
    source: str | None = None,
    limit: int = 25,
) -> list[dict]:
    """List stored jobs, newest first. `search` matches title/description/skills text;
    `since_hours` limits to jobs first seen recently (None = all); `source` filters by
    best_matches, most_recent or search:<query>."""
    p: dict[str, str] = {"order": "first_seen.desc", "limit": str(min(limit, 100))}
    if search:
        s = search.replace(",", " ").replace("*", "")
        p["or"] = f"(title.ilike.*{s}*,description.ilike.*{s}*,tile_text.ilike.*{s}*)"
    if since_hours:
        since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        p["first_seen"] = f"gte.{since.isoformat()}"
    if source:
        p["source"] = f"eq.{source}"
    return [_job_summary(j) for j in await db().select("jobs", p)]


@mcp.tool()
async def get_job(id_or_url: str, refresh: bool = False) -> dict:
    """Get full details for a job by id (~01...) or URL. Fetches the job page live via the
    worker if details aren't stored yet or refresh=True."""
    rows = await db().select("jobs", _id_filter(id_or_url))
    job = rows[0] if rows else None
    if job is None or refresh or not job.get("details"):
        url = id_or_url if id_or_url.startswith("http") else (job or {}).get("url") \
            or f"https://www.upwork.com/jobs/{id_or_url}"
        await _run("job_details", {"url": url})
        job = (await db().select("jobs", _id_filter(url)) or [job])[0]
    return {**job, "details": _clip(job.get("details"), 20_000)}


@mcp.tool()
async def live_search_jobs(
    query: str, sort: Literal["recency", "relevance"] = "recency", page: int = 1
) -> list[dict]:
    """Run a live Upwork job search in Chrome (results are also stored)."""
    res = await _run("search_jobs", {"query": query, "sort": sort, "page": page})
    return [_job_summary(j) for j in res["jobs"]]


@mcp.tool()
async def refresh_feed(name: Literal["best_matches", "most_recent"] = "best_matches") -> list[dict]:
    """Fetch the Find Work feed live from Chrome (results are also stored)."""
    res = await _run("feed", {"name": name})
    return [_job_summary(j) for j in res["jobs"]]


# ---- messages -------------------------------------------------------------

@mcp.tool()
async def list_threads(refresh: bool = False, limit: int = 30) -> list[dict]:
    """List message threads (stored). refresh=True re-syncs the inbox from Chrome first."""
    if refresh:
        await _run("sync_messages")
    rows = await db().select("threads", {"order": "last_seen.desc", "limit": str(limit),
                                         "select": "id,url,title,preview,last_seen,fetched_at"})
    return [{**r, "preview": _clip(r.get("preview"), 300)} for r in rows]


@mcp.tool()
async def read_thread(id_or_url: str, refresh: bool = True) -> dict:
    """Read a message thread's full text by room id (room_...) or URL. By default fetches
    it live; refresh=False returns the stored copy."""
    if refresh:
        url = id_or_url if id_or_url.startswith("http") else \
            f"https://www.upwork.com/ab/messages/rooms/{id_or_url}"
        await _run("read_thread", {"url": url})
        id_or_url = url
    rows = await db().select("threads", _id_filter(id_or_url))
    if not rows:
        raise ValueError("Thread not found; try refresh=True")
    return {**rows[0], "thread_text": _clip(rows[0].get("thread_text"), 30_000)}


@mcp.tool()
async def send_message(thread_id_or_url: str, text: str) -> dict:
    """Send a message in a thread. Only call after the user approved the exact text.
    The worker must have UPWORK_ALLOW_WRITE=1."""
    url = thread_id_or_url if thread_id_or_url.startswith("http") else \
        f"https://www.upwork.com/ab/messages/rooms/{thread_id_or_url}"
    return await _run("send_message", {"url": url, "text": text})


# ---- proposals ------------------------------------------------------------

@mcp.tool()
async def list_proposals(refresh: bool = False) -> list[dict]:
    """List your proposals (stored). refresh=True re-syncs from Chrome first."""
    if refresh:
        await _run("sync_proposals")
    rows = await db().select("proposals", {"order": "last_seen.desc", "limit": "100"})
    return [{**r, "row_text": _clip(r.get("row_text"), 400)} for r in rows]


# ---- generic --------------------------------------------------------------

@mcp.tool()
async def page_text(url: str) -> dict:
    """Fallback: fetch the visible text of any upwork.com page via Chrome."""
    res = await _run("page_text", {"url": url})
    return {**res, "text": _clip(res.get("text"), 30_000)}


def main() -> None:
    import sys
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    mcp.run(transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
