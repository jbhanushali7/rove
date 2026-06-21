import asyncio
import pytest

import rove.crawl as crawl_mod
import rove.mcp_jobs as jobs_mod
from rove.mcp_jobs import (
    _start_job_impl, _get_status_impl, _resolve_escalation_impl,
    _review_pending_action_impl, _stop_job_impl, _list_jobs_impl,
)


@pytest.fixture(autouse=True)
def clean_jobs():
    jobs_mod._JOBS.clear()
    yield
    jobs_mod._JOBS.clear()


async def _wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError("condition not met in time")
        await asyncio.sleep(0.01)


async def test_immediate_completion_reaches_done(monkeypatch):
    async def fake_run_crawl(url, **kwargs):
        return {"pages_crawled": 1, "stats": {"pages": 1}, "stop_reason": None}

    monkeypatch.setattr(crawl_mod, "run_crawl", fake_run_crawl)

    result = _start_job_impl("https://example.com")
    crawl_id = result["crawl_id"]
    await _wait_for(lambda: _get_status_impl(crawl_id)["status"] == "done")

    status = _get_status_impl(crawl_id)
    assert status["status"] == "done"
    assert status["stats"] == {"pages": 1}


async def test_run_crawl_exception_sets_error_status(monkeypatch):
    async def failing_run_crawl(url, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(crawl_mod, "run_crawl", failing_run_crawl)

    result = _start_job_impl("https://example.com")
    crawl_id = result["crawl_id"]
    await _wait_for(lambda: _get_status_impl(crawl_id)["status"] == "error")

    status = _get_status_impl(crawl_id)
    assert status["error"] == "boom"


async def test_escalation_question_flow(monkeypatch):
    captured = {}

    async def fake_run_crawl(url, *, prompt_channel, **kwargs):
        answer = await prompt_channel.ask("log in please", kind="escalation")
        captured["answer"] = answer
        return {"pages_crawled": 0, "stats": {}, "stop_reason": None}

    monkeypatch.setattr(crawl_mod, "run_crawl", fake_run_crawl)

    result = _start_job_impl("https://example.com")
    crawl_id = result["crawl_id"]
    await _wait_for(lambda: _get_status_impl(crawl_id)["status"] == "waiting_for_human")

    status = _get_status_impl(crawl_id)
    assert len(status["pending_questions"]) == 1
    question = status["pending_questions"][0]
    assert question["kind"] == "escalation"
    assert question["message"] == "log in please"

    resolve_result = _resolve_escalation_impl(crawl_id, question["question_id"], answer="")
    assert resolve_result == {"resolved": True}

    await _wait_for(lambda: _get_status_impl(crawl_id)["status"] == "done")
    assert captured["answer"] == ""
    assert _get_status_impl(crawl_id)["pending_questions"] == []


async def test_approval_question_flow(monkeypatch):
    captured = {}

    async def fake_run_crawl(url, *, prompt_channel, **kwargs):
        decision = await prompt_channel.ask("approve CLICK?", kind="approval")
        captured["decision"] = decision
        return {"pages_crawled": 0, "stats": {}, "stop_reason": None}

    monkeypatch.setattr(crawl_mod, "run_crawl", fake_run_crawl)

    result = _start_job_impl("https://example.com")
    crawl_id = result["crawl_id"]
    await _wait_for(lambda: _get_status_impl(crawl_id)["status"] == "waiting_for_human")

    question = _get_status_impl(crawl_id)["pending_questions"][0]
    assert question["kind"] == "approval"

    resolve_result = _review_pending_action_impl(crawl_id, question["question_id"], decision="e selector=#x")
    assert resolve_result == {"resolved": True}

    await _wait_for(lambda: _get_status_impl(crawl_id)["status"] == "done")
    assert captured["decision"] == "e selector=#x"


async def test_resolving_wrong_kind_returns_error(monkeypatch):
    async def fake_run_crawl(url, *, prompt_channel, **kwargs):
        await prompt_channel.ask("approve?", kind="approval")
        return {"pages_crawled": 0, "stats": {}, "stop_reason": None}

    monkeypatch.setattr(crawl_mod, "run_crawl", fake_run_crawl)

    result = _start_job_impl("https://example.com")
    crawl_id = result["crawl_id"]
    await _wait_for(lambda: _get_status_impl(crawl_id)["status"] == "waiting_for_human")
    question = _get_status_impl(crawl_id)["pending_questions"][0]

    error_result = _resolve_escalation_impl(crawl_id, question["question_id"], answer="")
    assert "error" in error_result

    # cleanup: resolve with the correct kind so the background task finishes
    _review_pending_action_impl(crawl_id, question["question_id"], decision="")
    await _wait_for(lambda: _get_status_impl(crawl_id)["status"] == "done")


async def test_start_job_returns_error_at_concurrency_cap(monkeypatch):
    monkeypatch.setattr(jobs_mod, "MAX_CONCURRENT_JOBS", 1)

    async def hanging_run_crawl(url, *, prompt_channel, stop_flag, **kwargs):
        await prompt_channel.ask("waiting forever", kind="escalation")
        return {"pages_crawled": 0, "stats": {}, "stop_reason": "stopped" if stop_flag.is_set() else None}

    monkeypatch.setattr(crawl_mod, "run_crawl", hanging_run_crawl)

    first = _start_job_impl("https://example.com")
    await _wait_for(lambda: _get_status_impl(first["crawl_id"])["status"] == "waiting_for_human")

    second = _start_job_impl("https://example.com/other")
    assert second == {"error": "max concurrent crawls reached", "limit": 1}

    # cleanup
    _stop_job_impl(first["crawl_id"])
    await _wait_for(lambda: _get_status_impl(first["crawl_id"])["status"] == "stopped")


async def test_stop_job_unblocks_pending_question(monkeypatch):
    captured = {}

    async def fake_run_crawl(url, *, prompt_channel, stop_flag, **kwargs):
        answer = await prompt_channel.ask("log in please", kind="escalation")
        captured["answer"] = answer
        return {"pages_crawled": 0, "stats": {}, "stop_reason": "stopped" if stop_flag.is_set() else None}

    monkeypatch.setattr(crawl_mod, "run_crawl", fake_run_crawl)

    result = _start_job_impl("https://example.com")
    crawl_id = result["crawl_id"]
    await _wait_for(lambda: _get_status_impl(crawl_id)["status"] == "waiting_for_human")

    stop_result = _stop_job_impl(crawl_id)
    assert stop_result == {"stopping": True}

    await _wait_for(lambda: _get_status_impl(crawl_id)["status"] == "stopped")
    assert captured["answer"] == ""


async def test_list_jobs_reports_all_started_jobs(monkeypatch):
    async def fake_run_crawl(url, **kwargs):
        return {"pages_crawled": 2, "stats": {"pages": 2}, "stop_reason": None}

    monkeypatch.setattr(crawl_mod, "run_crawl", fake_run_crawl)

    a = _start_job_impl("https://example.com/a")
    b = _start_job_impl("https://example.com/b")
    await _wait_for(lambda: _get_status_impl(a["crawl_id"])["status"] == "done")
    await _wait_for(lambda: _get_status_impl(b["crawl_id"])["status"] == "done")

    jobs = _list_jobs_impl()
    ids = {j["crawl_id"] for j in jobs}
    assert {a["crawl_id"], b["crawl_id"]} == ids
    assert all(j["pages_crawled"] == 2 for j in jobs)


def test_get_status_unknown_crawl_id_returns_error():
    assert _get_status_impl("nonexistent") == {"error": "unknown crawl_id"}


def test_resolve_escalation_unknown_crawl_id_returns_error():
    assert _resolve_escalation_impl("nonexistent", "q1", "") == {"error": "unknown crawl_id"}


def test_stop_job_unknown_crawl_id_returns_error():
    assert _stop_job_impl("nonexistent") == {"error": "unknown crawl_id"}
