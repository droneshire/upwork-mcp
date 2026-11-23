"""Local worker: runs next to your logged-in Chrome.

- Polls the Supabase `commands` queue and executes each command in Chrome.
- Every SYNC_INTERVAL_MIN minutes, syncs feeds, inbox and proposals.
- Writes a heartbeat to `worker_status` so the MCP server knows it's alive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .config import settings
from .db import DB
from .scraper import NotLoggedIn, Scraper

log = logging.getLogger("upwork-worker")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Worker:
    def __init__(self) -> None:
        self.db = DB()
        self.s = Scraper()
        self.logged_in: bool | None = None
        self.last_error: str | None = None

    # ---- persistence helpers -------------------------------------------

    async def save_jobs(self, jobs: list[dict]) -> int:
        ts = now()
        await self.db.upsert("jobs", [{**j, "last_seen": ts} for j in jobs])
        return len(jobs)

    async def save_threads(self, threads: list[dict]) -> int:
        ts = now()
        await self.db.upsert("threads", [{**t, "last_seen": ts} for t in threads])
        return len(threads)

    async def save_proposals(self, props: list[dict]) -> int:
        ts = now()
        await self.db.upsert("proposals", [{**p, "last_seen": ts} for p in props])
        return len(props)

    # ---- command handlers ----------------------------------------------

    async def run(self, ctype: str, args: dict[str, Any]) -> Any:
        s = self.s
        match ctype:
            case "feed":
                jobs = await s.feed(args.get("name", "best_matches"))
                await self.save_jobs(jobs)
                return {"jobs": jobs}
            case "search_jobs":
                jobs = await s.search_jobs(
                    args["query"], args.get("sort", "recency"), int(args.get("page", 1))
                )
                await self.save_jobs(jobs)
                return {"jobs": jobs}
            case "job_details":
                job = await s.job_details(args["url"])
                await self.db.upsert(
                    "jobs", [{**job, "details_fetched_at": now(), "last_seen": now()}]
                )
                return job
            case "sync_messages":
                threads = await s.threads()
                await self.save_threads(threads)
                return {"threads": threads}
            case "read_thread":
                t = await s.read_thread(args["url"])
                await self.db.upsert("threads", [{**t, "fetched_at": now(), "last_seen": now()}])
                return t
            case "sync_proposals":
                props = await s.proposals()
                await self.save_proposals(props)
                return {"proposals": props}
            case "send_message":
                if not settings.allow_write:
                    raise PermissionError(
                        "Writes disabled on worker; set UPWORK_ALLOW_WRITE=1 to allow sending"
                    )
                return await s.send_message(args["url"], args["text"])
            case "page_text":
                return await s.page_text(args["url"])
            case "ping":
                return {"pong": True, "logged_in": await s.is_logged_in()}
            case _:
                raise ValueError(f"unknown command type: {ctype}")

    async def full_sync(self) -> None:
        log.info("scheduled sync starting")
        for ctype, args in [
            ("feed", {"name": "best_matches"}),
            ("feed", {"name": "most_recent"}),
            ("sync_messages", {}),
            ("sync_proposals", {}),
        ]:
            try:
                await self.run(ctype, args)
                self.logged_in = True
            except NotLoggedIn as e:
                self.logged_in = False
                self.last_error = str(e)
                log.warning("%s", e)
                return
            except Exception as e:  # keep going with the rest
                self.last_error = f"{ctype}: {e}"
                log.exception("sync step %s failed", ctype)
        log.info("scheduled sync done")

    # ---- loops ----------------------------------------------------------

    async def heartbeat(self) -> None:
        await self.db.upsert("worker_status", [{
            "id": settings.worker_id,
            "last_heartbeat": now(),
            "logged_in": self.logged_in,
            "info": {"last_error": self.last_error, "allow_write": settings.allow_write,
                     "sync_interval_min": settings.sync_interval_min},
        }])

    async def process_one(self) -> bool:
        claimed = await self.db.rpc("claim_command")
        if not claimed:
            return False
        cmd = claimed[0]
        log.info("command %s %s %s", cmd["id"], cmd["type"], cmd["args"])
        try:
            result = await self.run(cmd["type"], cmd["args"] or {})
            self.logged_in = True
            await self.db.update("commands", {"id": f"eq.{cmd['id']}"},
                                 {"status": "done", "result": result, "finished_at": now()})
        except Exception as e:
            if isinstance(e, NotLoggedIn):
                self.logged_in = False
            log.exception("command %s failed", cmd["id"])
            await self.db.update("commands", {"id": f"eq.{cmd['id']}"},
                                 {"status": "error", "error": f"{type(e).__name__}: {e}",
                                  "finished_at": now()})
        return True

    async def main(self) -> None:
        await self.s.connect()
        log.info("attached to Chrome at %s", settings.cdp_url)
        await self.db.rpc("requeue_stale_commands", {"older_than_seconds": 300})
        interval = settings.sync_interval_min * 60
        next_sync = time.monotonic() if interval > 0 else float("inf")
        next_hb = 0.0
        try:
            while True:
                try:
                    if time.monotonic() >= next_hb:
                        await self.heartbeat()
                        next_hb = time.monotonic() + 30
                    if await self.process_one():
                        continue  # drain the queue before sleeping
                    if time.monotonic() >= next_sync:
                        await self.full_sync()
                        next_sync = time.monotonic() + interval
                        continue
                except Exception:
                    log.exception("worker loop error")
                await asyncio.sleep(settings.poll_seconds)
        finally:
            await self.s.close()
            await self.db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # queue polling is noisy
    asyncio.run(Worker().main())


if __name__ == "__main__":
    main()
