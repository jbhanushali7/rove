<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0B1020,50:6D28D9,100:2563EB&height=210&section=header&text=rove&fontSize=96&fontColor=ffffff&fontAlignY=38&animation=fadeIn&desc=autonomous%20web%20crawler%20with%20an%20agent%20at%20the%20wheel&descAlignY=60&descSize=18" alt="rove" />

<br/>

<a href="#"><img src="https://readme-typing-svg.demolab.com?font=Fira+Code&weight=600&size=22&duration=3200&pause=700&color=8B5CF6&center=true&vCenter=true&width=640&lines=Maps+any+site+into+a+graph.;An+LLM+agent+steers+the+crawl.;Stuck%3F+It+taps+you+on+the+shoulder." alt="tagline" /></a>

<br/>

![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-async-2EAD33?logo=playwright&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-site_graph-003B57?logo=sqlite&logoColor=white)
![License](https://img.shields.io/badge/license-Apache%202.0-blue)
![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)

</div>

---

## What is rove?

**rove** is an autonomous web crawler that explores a site like a rover explores terrain — and when it hits something it can't get past on its own (a login wall, a CAPTCHA, a form gate), an **LLM agent at mission control decides what to do and pulls a human into the loop**.

It renders JavaScript with a real browser, follows the most valuable links first, captures every interactive element (across iframes *and* shadow DOM), detects single-page-app state changes, and folds the whole thing into a queryable **site graph** in SQLite.

```
   the website            rove                        you
  ┌───────────┐     ┌────────────────┐            ┌────────────────┐
  │  pages,   │ ──► │ worker crawlers│ ──blocked──►   LLM master   │ ──stuck──► manual login
  │  forms,   │     │  (async, gated)│            │ agent decides  │            / one answer
  │  walls    │ ◄── │  site graph DB │ ◄─resume── └────────────────┘ ◄──session──┘
  └───────────┘     └────────────────┘
```

## Highlights

- 🧭 **Agent-steered crawling** — a pluggable LLM (Anthropic / OpenAI / local) watches the crawl, dismisses overlays, fills non-auth forms, deprioritizes dead sections, and decides *when it's genuinely stuck*.
- 🙋 **Human-in-the-loop, only when needed** — on a real login wall or CAPTCHA the agent opens a **visible browser** for you to log in (or asks one targeted question in the terminal), captures the session, injects it, and resumes — authenticated.
- 🕸️ **Deep extraction** — every link/button/input across **all frames and open shadow roots**, in a single evaluate per frame.
- ♿ **Accessibility tree** — each page captures a full ARIA snapshot (`a11y_tree`) and node count (`a11y_nodes`) alongside the DOM extraction.
- 🔀 **SPA-aware** — hooks `pushState`/`replaceState`/`hashchange` and fingerprints the DOM to record state transitions as graph edges.
- 🎯 **Priority frontier** — a scored `asyncio.PriorityQueue` crawls high-value pages (forms, functional paths) first.
- 🤖 **robots.txt respect** — fetches and obeys `robots.txt` at startup (including `Crawl-delay`). Use `--ignore-robots` to override.
- 🥷 **Stealth** — `playwright-stealth` patches ~20 headless-detection signals so bot-protected sites serve real content.
- 🗺️ **Queryable output** — pages, elements, and link/click edges land in a SQLite **site graph**. Export as JSON, Markdown, or **action-map** (states + transitions + element locators for browser agents).
- 🧬 **LLM schema extraction** — pass a JSON schema file and rove uses your LLM to pull structured data from every page after the crawl.
- 📡 **MCP server** — query the site graph from any MCP-compatible AI tool (Claude Desktop, Cursor, etc.) without writing SQL.
- 🔍 **Crawl diff** — compare two crawl runs to surface exactly what changed: new/removed pages, form mutations, new SPA states.
- 🔌 **Runs without an LLM too** — heuristics handle blockers offline; the agent is opt-in.
- 🆓 **Fully open source** — Apache 2.0 license, everything included, no paid tiers.

---

## Quickstart

### 1. Install

```bash
git clone https://github.com/<your-account>/rove.git
cd rove

python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
playwright install chromium
```

### 2. Crawl a site

Writes one JSON per page to `output/pages/` and one JPEG screenshot to `output/screenshots/`:

```bash
python -m rove.crawl --url https://example.com --max-pages 25 --concurrency 2
```

rove automatically fetches and respects `robots.txt` (including any `Crawl-delay` directive). To skip this:

```bash
python -m rove.crawl --url https://example.com --ignore-robots
```

### 3. Import into the site graph

Reads all page JSONs and populates SQLite at `output/db/site_graph.db`:

```bash
python -m rove.storage.db
```

### 4. Query the site graph

Use SQL directly:

```bash
python -c "import sqlite3; c=sqlite3.connect('output/db/site_graph.db'); print(c.execute('SELECT url, title FROM pages LIMIT 10').fetchall())"
```

Or run the MCP server and query from any MCP-compatible AI tool (see [MCP server](#mcp-server) below).

### 5. Let an LLM agent drive the crawl

Set your provider's API key, then pass `--master-provider`:

```bash
# Anthropic
set ANTHROPIC_API_KEY=sk-ant-...
python -m rove.crawl --url https://example.com --max-pages 30 \
  --master-provider anthropic --master-model claude-sonnet-4-6 --master-autonomy review

# OpenAI
set OPENAI_API_KEY=sk-...
python -m rove.crawl --url https://example.com --max-pages 30 \
  --master-provider openai --master-model gpt-4o --master-autonomy review
```

When the agent escalates (login wall / CAPTCHA), a browser window opens — log in, press `<kbd>`Enter`</kbd>`, and the crawl resumes with your session.

---

## The agent & human-in-the-loop

A cheap heuristic pre-filters **every** page, so the LLM is only consulted on pages that look blocked — keeping token cost bounded. The agent then picks one action:

| Action                  | What it does                                                           |
| ----------------------- | ---------------------------------------------------------------------- |
| `DISMISS_OVERLAY`     | close a cookie/consent/modal wall                                      |
| `FILL_FORM`           | fill a non-auth form (search/filter) — never password fields          |
| `CLICK`               | e.g. "continue as guest"                                               |
| `DEPRIORITIZE_PREFIX` | stop wasting budget on a dead section                                  |
| `STOP_CRAWL`          | end the run                                                            |
| `ESCALATE_HUMAN`      | hand off —`browser_login` (manual sign-in) or `terminal_question` |

Control how much the agent can do on its own with `--master-autonomy`:

- `auto` — the agent acts freely
- `review` *(default)* — write/escalate actions need your **approve / edit / skip / cancel**
- `manual` — every action is gated

Every decision, with the agent's reasoning, is logged to `output/agent_actions.md`. Captured sessions persist to `output/session.json` (git-ignored, written owner-only) and are reused on the next run.

---

## LLM schema extraction (`--schema`)

After a crawl you can use your LLM to pull **structured data** from every page's HTML, matched against a JSON schema you define. This is useful for extracting product details, article metadata, pricing, contact info, or anything else that appears consistently across pages.

### Step 1 — define your schema

Create a JSON schema file describing the fields you want:

```json
{
  "title": "string",
  "price": "string",
  "description": "string",
  "in_stock": "boolean"
}
```

### Step 2 — run the crawl with extraction

Pass the schema file and a master provider. Extraction runs automatically after the crawl completes:

```bash
python -m rove.crawl --url https://shop.example.com --max-pages 50 \
  --master-provider anthropic --master-model claude-sonnet-4-6 \
  --schema my_schema.json
```

### What you get

Each page JSON in `output/pages/` gains an `"extracted"` key containing the schema-matched data (or `null` if extraction failed for that page):

```json
{
  "url": "https://shop.example.com/products/widget",
  "title": "Widget",
  "extracted": {
    "title": "Widget",
    "price": "$29.99",
    "description": "A very fine widget.",
    "in_stock": true
  }
}
```

> **Note:** `--schema` requires `--master-provider` — the LLM must be configured. Without it, a warning is logged and extraction is skipped.

---

## MCP server

rove ships an **MCP server** (`rove-mcp`) with two kinds of tools: read-only graph queries over an already-finished crawl, and crawl-control tools that let an MCP client (Claude Code, Cursor, Antigravity, etc.) actually **start, steer, and resolve login walls / agent-action approvals for a live crawl** — turning the connected agent into the master agent's human-in-the-loop, no separate LLM key required (see below).

### Start the server

```bash
# If installed as a package:
rove-mcp

# Or directly:
python -m rove.mcp_server
```

The server reads/writes `output/` (DB, pages, screenshots) relative to its **working directory**. If you register it globally (so it's available from any IDE/project), pin that cwd — see [Global install](#global-install-any-ide-any-directory) below — otherwise crawl output scatters into whatever project folder happened to be open.

### Read-only tools (query a finished crawl)

| Tool                                       | What it does                                                      |
| ------------------------------------------ | ----------------------------------------------------------------- |
| `list_pages(limit, offset)`              | Paginate through all crawled pages with title, depth, fingerprint |
| `get_page(page_id)`                      | Full detail for one page: elements + outbound links               |
| `search_elements(query, limit)`          | Full-text search over element text and tags across all pages      |
| `find_path(from_url, to_url, max_depth)` | BFS shortest path between two URLs in the link graph              |

Run `python -m rove.storage.db` first to populate `output/db/site_graph.db` from a completed crawl's `output/pages/*.json`.

### Crawl-control tools (drive a live crawl)

| Tool                                                                                                                                                                                                          | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `start_crawl(url, max_pages, depth, concurrency, master_provider, master_model, master_autonomy, no_human_in_loop, ignore_robots, export, schema, headless, wait_until, block_resources, stagnation_limit)` | Launches a crawl as a background job. Returns `{"crawl_id": ...}` immediately. `headless=False` shows the browser window; `wait_until="networkidle"` waits for JS-rendered nav/links on SPAs that `domcontentloaded` misses; `block_resources` defaults to `["image", "font", "media"]` — pass `[]` to load everything; `stagnation_limit` defaults to `15` (pages in a row with no new element type before auto-stop) — pass `null`/`None` to crawl the whole site regardless of template repetition. |
| `get_crawl_status(crawl_id)`                                                                                                                                                                                | Live status (`starting`/`running`/`waiting_for_human`/`done`/`error`/`stopped`), stats, any `pending_questions`, recent `action_log`.                                                                                                                                                                                                                                                                                                                                                                          |
| `resolve_escalation(crawl_id, question_id, answer)`                                                                                                                                                         | Answer a login-wall/CAPTCHA/terminal escalation.`answer=""` means "I'm done logging in" for `browser_login`.                                                                                                                                                                                                                                                                                                                                                                                                               |
| `review_pending_action(crawl_id, question_id, decision)`                                                                                                                                                    | Approve (`""`), edit (`"e selector=#x"`), skip (`"s"`), or cancel (`"c"`) a pending agent action — same mini-language as the CLI's `--master-autonomy review/manual` prompt.                                                                                                                                                                                                                                                                                                                                        |
| `stop_crawl(crawl_id)`                                                                                                                                                                                      | Stops a job and unblocks any pending question with a safe default so it can drain.                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `list_jobs()`                                                                                                                                                                                               | Lists every crawl job started in this server process's lifetime, with status and page count.                                                                                                                                                                                                                                                                                                                                                                                                                                   |

Multiple concurrent jobs are allowed, capped by `ROVE_MAX_CONCURRENT_CRAWLS` (default `2`) — `start_crawl` returns `{"error": "max concurrent crawls reached", "limit": N}` past the cap.

**Let the connected IDE agent make the decisions instead of paying for a separate LLM key:** call `start_crawl(..., master_provider="none", master_autonomy="manual")`. The heuristic blocker still detects login walls/forms, but every action becomes a `pending_question` — your IDE's own model reads it via `get_crawl_status` and answers via `review_pending_action`/`resolve_escalation`. `master_provider`/`master_model` (and `OPENAI_API_KEY` / `LOCAL_LLM_BASE_URL`, see below) are only needed for `master_autonomy="auto"`, where the master must decide without round-tripping to a client on every action.

### Connect to Claude Desktop / Claude Code (project-scoped)

Add a `.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "rove": {
      "command": "venv\\Scripts\\python.exe",
      "args": ["-m", "rove.mcp_server"]
    }
  }
}
```

Then ask Claude things like:

- *"List the pages rove crawled on example.com"*
- *"Start a crawl of example.com and tell me when it needs me to log in"*
- *"What's the shortest path from the homepage to the checkout page?"*

### Global install (any IDE, any directory)

Since output paths are relative to cwd, pin it with a wrapper script (`rove-mcp.bat` at the repo root):

```bat
@echo off
cd /d "C:\path\to\rove"
"C:\path\to\rove\venv\Scripts\python.exe" -m rove.mcp_server
```

Register it at **user** scope so it's available regardless of which project your IDE has open:

```powershell
claude mcp add --scope user rove -- "C:\path\to\rove\rove-mcp.bat"
```

For Cursor/Antigravity/etc., point `command` at the absolute path to `rove-mcp.bat` in that tool's user-level MCP config file.

### Optional: LLM env vars (only for `master_autonomy="auto"`)

| Provider                                       | Env vars                                                                                                                                                                                                                            |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `anthropic`                                  | `ANTHROPIC_API_KEY`                                                                                                                                                                                                               |
| `openai`                                     | `OPENAI_API_KEY`                                                                                                                                                                                                                  |
| `local` (Ollama, vLLM, **NVIDIA NIM**) | `OPENAI_API_KEY` (the provider's key — NIM uses `OPENAI_API_KEY` too, just pointed elsewhere), `LOCAL_LLM_BASE_URL` (e.g. `https://integrate.api.nvidia.com/v1` for NIM, default `http://localhost:11434/v1` for Ollama) |

These are read by the MCP **server process** at the moment `start_crawl` runs, so set them before launching it (in your shell, or in the MCP client config's `env` block) — not inside the crawl request itself.

---

## Crawl diff (`rove-diff`)

Compare two crawl runs to see exactly what changed in a site's structure and interaction surface.

### When to use it

- Track site changes over time (new pages, removed pages, form mutations)
- Verify a site migration or redesign didn't break pages
- Detect new SPA states added by a feature release

### Usage

```bash
# Compare old and new crawl output pages/ directories
rove-diff output_old/pages output_new/pages

# Output as JSON only
rove-diff output_old/pages output_new/pages --format json

# Save to files instead of stdout
rove-diff output_old/pages output_new/pages --format both --output diff_report
# writes: diff_report.json and diff_report.md
```

Or without installation:

```bash
python -m rove.diff output_old/pages output_new/pages
```

### What it detects

| Category                     | Description                                                         |
| ---------------------------- | ------------------------------------------------------------------- |
| **Added pages**        | URLs present in the new crawl but not the old                       |
| **Removed pages**      | URLs present in the old crawl but not the new                       |
| **Changed pages**      | Same URL, different DOM fingerprint — the page's structure changed |
| **Added forms**        | New forms (identified by action + method + field names)             |
| **Removed forms**      | Forms that disappeared                                              |
| **Added SPA states**   | New click-triggered UI states (tabs, menus, modals)                 |
| **Removed SPA states** | SPA states that no longer exist                                     |

### Sample Markdown output

```markdown
# Crawl Diff

## Added Pages (2)
- `https://example.com/new-feature`
- `https://example.com/pricing`

## Removed Pages (1)
- `https://example.com/old-page`

## Changed Pages (fingerprint) (3)
- `https://example.com/home`
...
```

### Use from Python

```python
from rove.diff import diff_crawls, diff_to_dict, diff_to_markdown

d = diff_crawls("output_old/pages", "output_new/pages")
print(f"Added: {len(d.added_pages)}, Removed: {len(d.removed_pages)}")
print(diff_to_markdown(d))
```

---

## Accessibility tree

Every crawled page automatically captures Playwright's ARIA snapshot — a YAML representation of the accessibility tree. It's stored in the page JSON alongside the DOM extraction:

```json
{
  "url": "https://example.com/",
  "a11y_tree": "- document \"Example Domain\"\n  - heading \"Example Domain\" [level=1]\n  - paragraph\n  - link \"More information...\"",
  "a11y_nodes": 4
}
```

`a11y_nodes` is a quick count of accessibility nodes (useful for scoring page richness). The full `a11y_tree` YAML is available for downstream processing or LLM prompting. No extra flag is needed — it's always captured.

---

## CLI reference

### `rove` (crawler)

```bash
python -m rove.crawl [options]
# or, if installed: rove [options]
```

| Flag                   | Default              | Description                                                                                                                                                                              |
| ---------------------- | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--url`              | *(required)*       | Seed URL — use the canonical host, e.g.`https://www.example.com/`                                                                                                                     |
| `--max-pages`        | `50`               | Page budget                                                                                                                                                                              |
| `--depth`            | `3`                | Max crawl depth — the seed page is depth 0; pages up to and including depth `N` are crawled and have their own links followed, so `--depth 3` reaches pages at depth 0, 1, 2, and 3 |
| `--concurrency`      | `2`                | Parallel tabs (hard cap: 3)                                                                                                                                                              |
| `--ignore-robots`    | off                  | Skip fetching / obeying `robots.txt`                                                                                                                                                   |
| `--master-provider`  | `none`             | LLM provider:`none` · `anthropic` · `openai` · `local`                                                                                                                        |
| `--master-model`     | —                   | Model ID for the chosen provider                                                                                                                                                         |
| `--master-autonomy`  | `review`           | `auto` · `review` · `manual`                                                                                                                                                     |
| `--no-human-in-loop` | off                  | Disable the master agent entirely (for CI / automated runs)                                                                                                                              |
| `--export`           | —                   | Export format(s):`json` · `markdown` · `action-map` (repeatable)                                                                                                                 |
| `--schema`           | —                   | JSON schema file for LLM data extraction (requires `--master-provider`)                                                                                                                |
| `--headed`           | off                  | Show the Chromium window instead of running headless                                                                                                                                     |
| `--wait-until`       | `domcontentloaded` | `domcontentloaded` (fast) or `networkidle` — use `networkidle` on JS-rendered SPAs where nav links only appear after client-side routing finishes                                 |
| `--block-resources`  | `image,font,media` | Comma-separated Playwright resource types to abort. Pass `none` to disable blocking and load everything (slower, more memory, but useful when verifying visually in `--headed` mode) |
| `--stagnation-limit` | `15`               | Stop after this many pages in a row contribute no new element type. Pass `unlimited` to disable and crawl the whole site regardless of how repetitive the page templates are           |

### `rove-diff` (crawl differ)

```bash
rove-diff <old_pages_dir> <new_pages_dir> [options]
# or: python -m rove.diff <old_pages_dir> <new_pages_dir> [options]
```

| Flag         | Default  | Description                                                                |
| ------------ | -------- | -------------------------------------------------------------------------- |
| `--format` | `both` | Output format:`json` · `markdown` · `both`                         |
| `--output` | stdout   | Output file path stem (extensions `.json` / `.md` added automatically) |

### `rove-mcp` (MCP server)

```bash
rove-mcp
# or: python -m rove.mcp_server
```

No CLI flags of its own — reads/writes `output/` relative to its working directory. Crawl-control behavior is configured per-call via the `start_crawl` tool's arguments (same names/defaults as the `rove` CLI flags above) and one env var:

| Env var                        | Default | Description                                                     |
| ------------------------------ | ------- | --------------------------------------------------------------- |
| `ROVE_MAX_CONCURRENT_CRAWLS` | `2`   | Cap on simultaneous `start_crawl` jobs in this server process |

---

## How it works

Two independent phases — **crawl** (writes JSON) then **import** (reads JSON into SQLite); nothing touches the DB while crawling.

**Crawl** — a pool of async workers shares one Chromium instance; parallelism comes from multiple tabs gated by a semaphore. At startup, `robots.txt` is fetched and any `Crawl-delay` is honoured (one worker sleeps at a time so requests stagger naturally). The frontier is a scored priority queue; each page is extracted, screenshotted, ARIA-snapshotted, fingerprinted, and probed for SPA states. A coordinator adapts crawl behaviour every 20 pages; the master agent handles blockers on demand.

**Import** — a two-pass loader inserts pages (identity = `UNIQUE(url, fingerprint)`, so the same URL with a different DOM is a distinct state node) then wires up element rows and link/click edges.

**Schema extraction** — runs after the crawl if `--schema` is provided. The LLM reads each page's HTML (up to 8 000 chars) and returns JSON matching the schema you defined. Results are written back into each page JSON as an `"extracted"` key.

```
output/
  pages/            one JSON per crawled page / SPA state
  screenshots/      one JPEG per page / state
  db/
    site_graph.db   SQLite: pages · elements · links
  agent_actions.md  what the master agent decided, and why
  crawl_log.md      coordinator summary every 20 pages
  session.json      captured auth session (git-ignored)
```

---

## Testing

```bash
# Full suite
venv\Scripts\pytest -v          # Windows
pytest -v                       # macOS / Linux

# A single file
pytest tests/test_diff.py -v
pytest tests/test_blocker.py -v
```

`pytest.ini` sets `asyncio_mode = auto`, so async tests need no decorators. The extraction test spins up a local `http.server` and verifies elements are pulled from both an iframe and an open shadow root.

---

## Roadmap

rove is **fully open source** under the Apache 2.0 license.

## License

Apache 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
