"""Minimal async Supabase (PostgREST) client."""

from __future__ import annotations

from typing import Any

import httpx

from .config import settings


class SupabaseError(RuntimeError):
    pass


class DB:
    def __init__(self, url: str | None = None, key: str | None = None) -> None:
        url = url or settings.supabase_url
        key = key or settings.supabase_key
        if not url or not key:
            raise SupabaseError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/rest/v1",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        r = await self._client.request(method, path, **kw)
        if r.status_code >= 400:
            raise SupabaseError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else None

    async def select(self, table: str, params: dict[str, str] | None = None) -> list[dict]:
        return await self._req("GET", f"/{table}", params=params or {})

    async def insert(self, table: str, row: dict) -> dict:
        rows = await self._req(
            "POST", f"/{table}", json=row, headers={"Prefer": "return=representation"}
        )
        return rows[0]

    async def upsert(self, table: str, rows: list[dict], on_conflict: str = "id") -> None:
        if not rows:
            return
        # PostgREST requires every row in a batch to have the same keys.
        keys = set().union(*rows)
        rows = [{k: r.get(k) for k in keys} for r in rows]
        await self._req(
            "POST",
            f"/{table}",
            params={"on_conflict": on_conflict},
            json=rows,
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )

    async def update(self, table: str, filters: dict[str, str], values: dict) -> None:
        await self._req(
            "PATCH", f"/{table}", params=filters, json=values,
            headers={"Prefer": "return=minimal"},
        )

    async def rpc(self, fn: str, args: dict | None = None) -> Any:
        return await self._req("POST", f"/rpc/{fn}", json=args or {})
