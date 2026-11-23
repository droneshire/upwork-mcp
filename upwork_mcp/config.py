from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Explicit env file, else the project's .env (works regardless of cwd), else cwd.
load_dotenv(os.getenv("UPWORK_MCP_ENV") or Path(__file__).resolve().parent.parent / ".env")
load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_key: str = os.getenv("SUPABASE_SERVICE_KEY", "")
    # Worker
    cdp_url: str = os.getenv("CHROME_CDP_URL", "http://127.0.0.1:9222")
    worker_id: str = os.getenv("WORKER_ID", "local")
    poll_seconds: float = float(os.getenv("POLL_SECONDS", "3"))
    sync_interval_min: float = float(os.getenv("SYNC_INTERVAL_MIN", "15"))  # 0 disables
    allow_write: bool = _bool("UPWORK_ALLOW_WRITE")
    # MCP server
    command_timeout: float = float(os.getenv("COMMAND_TIMEOUT", "120"))
    worker_stale_seconds: float = float(os.getenv("WORKER_STALE_SECONDS", "120"))


settings = Settings()
