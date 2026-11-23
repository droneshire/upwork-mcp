# upwork-mcp

An MCP server for your Upwork account. It runs through **your own logged-in Chrome**, because Upwork and Cloudflare block headless and cloud browsers. Data is stored in **Supabase**, and commands pass through it too, so the MCP server can run anywhere, including on cloud machines.

```
 Claude ──MCP──> upwork-mcp (anywhere) ──> Supabase <── upwork-worker (your PC) ──CDP──> Chrome (logged in)
                 reads tables,                tables +       polls commands,
                 queues commands              command queue  scheduled sync
```

- **Worker** (`upwork-worker`) runs on the machine with Chrome. It attaches over the Chrome DevTools Protocol in its own tab and never closes your browser. Every `SYNC_INTERVAL_MIN` minutes it syncs Best Matches, Most Recent, your inbox and your proposals. It also runs queued commands as they arrive.
- **MCP server** (`upwork-mcp`) only talks to Supabase. Reads come from the stored tables. Live actions are queued and the server waits for the worker's result.

## Setup

### 1. Supabase
Create a project, then run [`supabase/schema.sql`](supabase/schema.sql) in the SQL editor. RLS is enabled with no policies, so only the service-role key has access.

### 2. Install
```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[worker]"   # the MCP-only host can skip [worker]
cp .env.example .env                    # fill SUPABASE_URL / SUPABASE_SERVICE_KEY
```

### 3. Chrome and worker (on your PC)
```bash
./scripts/start-chrome.sh
```
Log into Upwork in that window once. It uses its own profile, `~/.upwork-chrome-profile`, because Chrome 136+ blocks remote debugging on the default profile. Then:
```bash
.venv/bin/upwork-worker
```

### 4. Register the MCP server
```bash
claude mcp add upwork -- /path/to/upwork-mcp/.venv/bin/upwork-mcp
```
The server reads `.env` from its working directory. You can also pass `-e SUPABASE_URL=... -e SUPABASE_SERVICE_KEY=...`. For HTTP transport, run `upwork-mcp streamable-http`.

## Tools

| Tool | Source |
|---|---|
| `worker_status` | Worker heartbeat and login state |
| `list_jobs(search, since_hours, source, limit)` | Stored |
| `get_job(id_or_url, refresh)` | Stored; fetched live if details are missing |
| `live_search_jobs(query, sort, page)` | Live |
| `refresh_feed(best_matches\|most_recent)` | Live |
| `list_threads(refresh)` | Stored, optional live resync |
| `read_thread(id_or_url, refresh=True)` | Live |
| `list_proposals(refresh)` | Stored, optional live resync |
| `send_message(thread, text)` | Live; the worker must have `UPWORK_ALLOW_WRITE=1` |
| `page_text(url)` | Live, any upwork.com page (fallback) |

## Notes
- Parsing uses Upwork's `data-test` attributes, checked against the live site in Nov 2026, and falls back to raw page text. If Upwork changes its markup, the fields in `upwork_mcp/scraper.py` may need updating.
- The worker paces page loads with random delays. Even so, automating Upwork is against its ToS, so keep usage light.
- `send_message` is off by default. Submitting proposals isn't implemented, because it spends Connects.
- Keep `.env` private: the service-role key bypasses RLS.
