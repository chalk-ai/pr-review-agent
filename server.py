"""Web server for the PR review agent demo.

GET  /                         the demo page (index.html); ?embed=1 hides the page header
GET  /api/examples             featured pull requests
POST /api/reviews {"url"}      starts a review, or replays a cached one; returns {"id"}
GET  /api/reviews/{id}/events  server-sent events for that review

Each fresh review runs pipeline.run_review in a worker thread. Finished reviews are cached by
repo and head SHA (in memory and on a Chalk volume), so a repeat request replays the recorded
events at their original pace instead of spending another agent run.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from chalkcompute import Sandbox, Volume, VolumeFileNotFoundError
from pipeline import ReviewError, RouterConfig, Target, resolve, run_review

PORT = int(os.environ.get("PORT", "8080"))
INDEX_HTML = Path(__file__).with_name("index.html")
MAX_CONCURRENT = int(os.environ.get("REVIEW_MAX_CONCURRENT", "2"))
MAX_FRESH_PER_HOUR = int(os.environ.get("REVIEW_MAX_PER_HOUR", "20"))
REVIEW_TIMEOUT = int(os.environ.get("REVIEW_TIMEOUT", "480"))
CACHE_VOLUME = os.environ.get("REVIEW_CACHE_VOLUME", "pr-review-demo-cache")
# Replays keep the recorded rhythm but never sit idle longer than this between events.
REPLAY_MAX_GAP_S = 1.2
# Readers poll a job's append-only event list; cheap at demo scale and immune to missed wakeups.
POLL_S = 0.1
EXAMPLES = [
    {"label": "markupsafe · Rust speedups", "url": "https://github.com/pallets/markupsafe/pull/528"},
    {"label": "requests · open PR", "url": "https://github.com/psf/requests"},
    {"label": "httpx · open PR", "url": "https://github.com/encode/httpx"},
]

log = logging.getLogger("pr-review")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def router_config() -> RouterConfig:
    api = os.environ["CHALK_API_SERVER"].rstrip("/")
    return RouterConfig(
        base_url=os.environ.get("REVIEW_ROUTER_BASE_URL", f"{api}/v1/router"),
        environment_id=os.environ["CHALK_ENVIRONMENT"],
        key_secret=os.environ.get("REVIEW_KEY_SECRET", "CHALK_ROUTER_API_KEY"),
        model=os.environ.get("REVIEW_MODEL", "anthropic/claude-sonnet-5-5"),
    )


class Job:
    """One review's event log. Readers follow it from the start while it is still growing."""

    def __init__(self, key: str, replay: bool) -> None:
        super().__init__()
        self.id = uuid.uuid4().hex[:12]
        self.key = key
        self.replay = replay
        self.events: list[dict[str, Any]] = []
        self.done = False
        self.started = time.monotonic()

    def push(self, event: dict[str, Any]) -> None:
        event.setdefault("at", round(time.monotonic() - self.started, 2))
        self.events.append(event)

    def finish(self) -> None:
        self.done = True

    async def wait_done(self) -> None:
        while not self.done:
            await asyncio.sleep(POLL_S)


class ReviewCache:
    """Finished event logs, keyed by repo@sha, mirrored to a Chalk volume across restarts."""

    def __init__(self) -> None:
        super().__init__()
        self.memory: dict[str, list[dict[str, Any]]] = {}
        self.lock = threading.Lock()

    @staticmethod
    def path(key: str) -> str:
        return f"reviews/{hashlib.sha256(key.encode()).hexdigest()[:24]}.json"

    def get(self, key: str) -> list[dict[str, Any]] | None:
        with self.lock:
            if key in self.memory:
                return self.memory[key]
        try:
            events = json.loads(Volume(CACHE_VOLUME).read_file(self.path(key)))
        except (VolumeFileNotFoundError, ValueError):
            return None
        except Exception:  # noqa: BLE001 - a cold cache is fine; log and run fresh
            log.exception("cache read failed for %s", key)
            return None
        with self.lock:
            self.memory[key] = events
        return events

    def put(self, key: str, events: list[dict[str, Any]]) -> None:
        with self.lock:
            self.memory[key] = events
        try:
            Volume(CACHE_VOLUME).put_file(self.path(key), json.dumps(events))
        except Exception:  # noqa: BLE001 - the in-memory copy still serves replays
            log.exception("cache write failed for %s", key)


state: dict[str, Any] = {}


def cache_key(target: Target) -> str:
    return f"{target.repo}@{target.head_sha}"


def is_complete(events: list[dict[str, Any]]) -> bool:
    return any(e.get("t") == "review" for e in events) and not any(e.get("t") == "error" for e in events)


def run_fresh(job: Job, target: Target, loop: asyncio.AbstractEventLoop) -> None:
    """Worker thread: runs the pipeline and hands each event to the event loop."""
    try:
        for event in run_review(target, router_config(), REVIEW_TIMEOUT):
            loop.call_soon_threadsafe(job.push, event)
    except ReviewError as exc:
        loop.call_soon_threadsafe(job.push, {"t": "error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - reported to the viewer
        log.exception("review failed for %s", job.key)
        loop.call_soon_threadsafe(job.push, {"t": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        loop.call_soon_threadsafe(job.finish)


async def replay(job: Job, events: list[dict[str, Any]]) -> None:
    last = 0.0
    for event in events:
        at = float(event.get("at", last))
        await asyncio.sleep(min(max(at - last, 0.0), REPLAY_MAX_GAP_S))
        last = at
        job.push({k: v for k, v in event.items() if k != "at"})
    job.finish()


async def watch_fresh(job: Job, slots: asyncio.Semaphore) -> None:
    try:
        await job.wait_done()
        if is_complete(job.events):
            await asyncio.to_thread(state["cache"].put, job.key, job.events)
    finally:
        state["running"].pop(job.key, None)
        slots.release()


async def start_review(url: str) -> Job:
    target = await asyncio.to_thread(resolve, url)
    key = cache_key(target)
    if (running := state["running"].get(key)) is not None:
        return running
    if (cached := await asyncio.to_thread(state["cache"].get, key)) is not None:
        job = Job(key, replay=True)
        state["jobs"][job.id] = job
        asyncio.create_task(replay(job, cached))
        return job

    slots: asyncio.Semaphore = state["slots"]
    recent: collections.deque[float] = state["recent"]
    now = time.monotonic()
    while recent and now - recent[0] > 3600:
        recent.popleft()
    if len(recent) >= MAX_FRESH_PER_HOUR:
        raise ReviewError(
            "The demo has run its hourly quota of fresh reviews. Pick an example; those replay instantly."
        )
    if slots.locked():
        raise ReviewError("Every review slot is busy right now. Try again in a minute, or pick an example.")
    await slots.acquire()
    recent.append(now)
    job = Job(key, replay=False)
    state["jobs"][job.id] = job
    state["running"][key] = job
    threading.Thread(target=run_fresh, args=(job, target, asyncio.get_running_loop()), daemon=True).start()
    asyncio.create_task(watch_fresh(job, slots))
    return job


def reap_leftover_sandboxes() -> None:
    """Terminates review sandboxes a previous replica left behind; their lifetime would end them too."""
    try:
        leftovers = [info for info in Sandbox.list_all() if (info.name or "").startswith("pr-review-")]
    except Exception:  # noqa: BLE001 - best effort
        log.exception("listing sandboxes failed")
        return
    for info in leftovers:
        try:
            Sandbox.from_id(info.id).terminate()
        except Exception:  # noqa: BLE001 - already gone, or terminating on its own
            log.info("skipped leftover sandbox %s (%s)", info.id, info.status)


async def prewarm_examples() -> None:
    """Runs each featured example once, one at a time, so the first visitors get instant replays."""
    for example in EXAMPLES:
        try:
            job = await start_review(example["url"])
            await job.wait_done()
            log.info("example %s ready (%s)", example["url"], "replay" if job.replay else "fresh")
        except Exception:  # noqa: BLE001 - examples fall back to running on demand
            log.exception("prewarm failed for %s", example["url"])


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    state.update(
        jobs={},
        running={},
        cache=ReviewCache(),
        slots=asyncio.Semaphore(MAX_CONCURRENT),
        recent=collections.deque(),
    )
    await asyncio.to_thread(reap_leftover_sandboxes)
    prewarm = asyncio.create_task(prewarm_examples()) if os.environ.get("REVIEW_PREWARM", "1") == "1" else None
    yield
    if prewarm is not None:
        prewarm.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text())


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "running": len(state["running"])})


@app.get("/api/examples")
async def examples() -> JSONResponse:
    return JSONResponse(EXAMPLES)


@app.post("/api/reviews")
async def create_review(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        url = str(body.get("url", ""))[:300]
        job = await start_review(url)
    except ReviewError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except (ValueError, AttributeError):
        return JSONResponse(
            {"error": 'Send JSON like {"url": "https://github.com/owner/repo/pull/1"}.'}, status_code=400
        )
    except Exception as exc:  # noqa: BLE001 - surface GitHub/network failures to the viewer
        log.exception("could not start review")
        return JSONResponse({"error": f"Could not start the review: {exc}"}, status_code=502)
    return JSONResponse({"id": job.id, "replay": job.replay})


@app.get("/api/reviews/{job_id}/events")
async def review_events(job_id: str) -> StreamingResponse:
    job: Job | None = state["jobs"].get(job_id)

    async def stream() -> AsyncIterator[str]:
        if job is None:
            yield f"data: {json.dumps({'t': 'error', 'message': 'Unknown review.'})}\n\n"
            return
        yield f"data: {json.dumps({'t': 'hello', 'replay': job.replay})}\n\n"
        sent = 0
        idle = 0.0
        while True:
            while sent < len(job.events):
                yield f"data: {json.dumps(job.events[sent])}\n\n"
                sent += 1
                idle = 0.0
            if job.done:
                yield f"data: {json.dumps({'t': 'end'})}\n\n"
                return
            await asyncio.sleep(POLL_S)
            idle += POLL_S
            if idle >= 15:
                yield ": keepalive\n\n"
                idle = 0.0

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
