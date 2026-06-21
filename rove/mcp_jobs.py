"""In-memory registry of MCP-triggered crawl jobs.

Lets an MCP client start a crawl, poll its status, and resolve escalations
(login walls / CAPTCHAs / terminal questions) and action approvals through MCP
tool calls instead of the crawl blocking on terminal input(). Each job owns its
own Playwright browser/context — that lifecycle already lives entirely inside
rove.crawl.run_crawl(), so multiple jobs running concurrently need no special
handling here beyond a safety cap (MAX_CONCURRENT_JOBS) to avoid exhausting RAM
on a constrained host.

rove.crawl is imported lazily (inside _run_job) so that rove/mcp_server.py stays
importable — and usable for its read-only DB-query tools — without requiring
Playwright to be installed.
"""
import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

from rove.prompt_channel import PromptChannel

logger = logging.getLogger(__name__)

MAX_CONCURRENT_JOBS = int(os.environ.get("ROVE_MAX_CONCURRENT_CRAWLS", "2"))
_ACTIVE_STATUSES = {"starting", "running", "waiting_for_human"}


@dataclass
class PendingQuestion:
    question_id: str
    kind: str          # "escalation" | "approval"
    message: str
    future: asyncio.Future


@dataclass
class CrawlJob:
    crawl_id: str
    task: asyncio.Task | None = None
    status: str = "starting"               # starting|running|waiting_for_human|done|error|stopped
    stats: dict = field(default_factory=dict)
    pending_questions: dict[str, PendingQuestion] = field(default_factory=dict)
    action_log: list[dict] = field(default_factory=list)
    error: str | None = None
    stop_flag: asyncio.Event = field(default_factory=asyncio.Event)
    created_at: float = field(default_factory=time.monotonic)


# Module-level — this process IS the MCP server; jobs live for its lifetime.
_JOBS: dict[str, CrawlJob] = {}


class MCPPromptChannel(PromptChannel):
    """Surfaces master-agent questions as pending MCP tool calls instead of stdin.

    Unlike TerminalPromptChannel, this channel is always "interactive" — there's no
    notion of a missing terminal here; a connected MCP client can always eventually
    answer, so escalations always become a pending question rather than being
    silently skipped.
    """

    def __init__(self, job: CrawlJob):
        self._job = job

    def is_interactive(self) -> bool:
        return True

    async def ask(self, message: str, *, kind: str = "escalation") -> str:
        loop = asyncio.get_event_loop()
        question_id = uuid.uuid4().hex[:12]
        question = PendingQuestion(question_id=question_id, kind=kind, message=message,
                                    future=loop.create_future())
        self._job.pending_questions[question_id] = question
        self._job.status = "waiting_for_human"
        try:
            response = await question.future
            self._job.action_log.append({"kind": kind, "message": message, "response": response})
            return response
        finally:
            self._job.pending_questions.pop(question_id, None)
            if not self._job.pending_questions and self._job.status == "waiting_for_human":
                self._job.status = "running"


def _active_job_count() -> int:
    return sum(1 for j in _JOBS.values() if j.status in _ACTIVE_STATUSES)


async def _run_job(job: CrawlJob, url: str, kwargs: dict) -> None:
    from rove.crawl import run_crawl
    from rove import coordinator

    channel = MCPPromptChannel(job)

    def _progress(stats):
        job.stats = coordinator.stats_snapshot(stats)

    job.status = "running"
    try:
        result = await run_crawl(
            url, prompt_channel=channel, stop_flag=job.stop_flag, progress_cb=_progress, **kwargs)
        job.stats = result.get("stats", job.stats)
        job.status = "stopped" if result.get("stop_reason") else "done"
    except Exception as e:
        logger.error(f"[mcp_jobs] crawl {job.crawl_id} failed: {e}")
        job.error = str(e)
        job.status = "error"


def _start_job_impl(url: str, **kwargs) -> dict:
    """Launch a crawl as a background task. kwargs are forwarded to run_crawl()
    (max_pages, depth, concurrency, master_provider, master_model, master_autonomy,
    no_human_in_loop, ignore_robots, export, schema)."""
    if _active_job_count() >= MAX_CONCURRENT_JOBS:
        return {"error": "max concurrent crawls reached", "limit": MAX_CONCURRENT_JOBS}

    crawl_id = uuid.uuid4().hex[:12]
    job = CrawlJob(crawl_id=crawl_id)
    _JOBS[crawl_id] = job
    job.task = asyncio.create_task(_run_job(job, url, kwargs))
    return {"crawl_id": crawl_id}


def _get_status_impl(crawl_id: str) -> dict:
    job = _JOBS.get(crawl_id)
    if job is None:
        return {"error": "unknown crawl_id"}
    return {
        "crawl_id": job.crawl_id,
        "status": job.status,
        "stats": job.stats,
        "pending_questions": [
            {"question_id": q.question_id, "kind": q.kind, "message": q.message}
            for q in job.pending_questions.values()
        ],
        "action_log": job.action_log[-20:],
        "error": job.error,
    }


def _resolve_question_impl(crawl_id: str, question_id: str, kind: str, response: str) -> dict:
    job = _JOBS.get(crawl_id)
    if job is None:
        return {"error": "unknown crawl_id"}
    question = job.pending_questions.get(question_id)
    if question is None:
        return {"error": "unknown or already-resolved question_id"}
    if question.kind != kind:
        return {"error": f"question {question_id} is kind={question.kind!r}, not {kind!r}"}
    if not question.future.done():
        question.future.set_result(response)
    return {"resolved": True}


def _resolve_escalation_impl(crawl_id: str, question_id: str, answer: str = "") -> dict:
    """answer: free-text response, or "" to mean "I'm done" for a browser-login wait."""
    return _resolve_question_impl(crawl_id, question_id, "escalation", answer)


def _review_pending_action_impl(crawl_id: str, question_id: str, decision: str = "") -> dict:
    """decision: the same mini-language actions.human_review() already parses —
    "" approve, "e k=v,.." edit, "s" skip, "c" cancel crawl."""
    return _resolve_question_impl(crawl_id, question_id, "approval", decision)


def _stop_job_impl(crawl_id: str) -> dict:
    job = _JOBS.get(crawl_id)
    if job is None:
        return {"error": "unknown crawl_id"}
    job.stop_flag.set()
    # Unblock any pending question with a safe default so the crawl can actually
    # drain instead of hanging on a future nothing will ever resolve.
    for q in list(job.pending_questions.values()):
        if not q.future.done():
            q.future.set_result("" if q.kind == "escalation" else "c")
    return {"stopping": True}


def _list_jobs_impl() -> list[dict]:
    return [
        {"crawl_id": j.crawl_id, "status": j.status, "pages_crawled": j.stats.get("pages", 0)}
        for j in _JOBS.values()
    ]
