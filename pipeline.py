"""The review pipeline: GitHub link -> volume -> sandbox -> Claude Agent SDK -> feedback.

Each step yields plain dict events, so the same code drives the web server (which relays
them to the browser) and review_cli.py (which prints them).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from chalkcompute import (
    ContainerSecurityPolicy,
    Image,
    KernelPolicy,
    NetworkPolicy,
    Sandbox,
    Secret,
    Volume,
    VolumeNotFoundError,
)

HERE = Path(__file__).resolve().parent
AGENT_SDK_VERSION = "0.2.165"
REVIEW_ROOT = "/review"

# Size limits keep one visitor from tying up the demo with a monorepo.
MAX_REPO_KB = 400_000
MAX_DIFF_BYTES = 300_000
MAX_CHANGED_FILES = 120
MAX_STAGED_FILES = 6_000
MAX_STAGED_BYTES = 80_000_000
MAX_FILE_BYTES = 400_000
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", ".venv", "__pycache__", "target"}

GITHUB_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9-]{1,39})/(?P<repo>[A-Za-z0-9._-]{1,100})"
    + r"(?:/(?:pull|pulls)/(?P<number>\d+)|/commit/(?P<sha>[0-9a-f]{7,40}))?/?(?:[?#].*)?$"
)


class ReviewError(Exception):
    """A problem the visitor can act on (bad link, private repo, too large)."""


@dataclass
class Target:
    repo: str  # owner/name
    kind: str  # "pull" or "commit"
    head_sha: str
    title: str
    body: str
    url: str
    number: int | None = None
    base_sha: str | None = None
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    diff: str = field(default="", repr=False)

    @property
    def label(self) -> str:
        return f"#{self.number}" if self.kind == "pull" else self.head_sha[:7]

    def meta(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "kind": self.kind,
            "number": self.number,
            "label": self.label,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "additions": self.additions,
            "deletions": self.deletions,
            "changed_files": self.changed_files,
        }


def github_client() -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "chalk-pr-review-demo"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url="https://api.github.com", headers=headers, timeout=20, follow_redirects=True)


def _get(gh: httpx.Client, path: str, **params: Any) -> Any:
    resp = gh.get(path, params=params)
    if resp.status_code == 404:
        raise ReviewError("GitHub could not find that repository. Only public repositories work.")
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        raise ReviewError("GitHub's API rate limit is exhausted for now. Try again in a few minutes.")
    resp.raise_for_status()
    return resp.json()


def _fetch_diff(url: str) -> str:
    resp = httpx.get(url, follow_redirects=True, timeout=30, headers={"User-Agent": "chalk-pr-review-demo"})
    resp.raise_for_status()
    if len(resp.content) > MAX_DIFF_BYTES:
        raise ReviewError(
            f"That diff is {len(resp.content) // 1000} KB; the demo reviews diffs up to {MAX_DIFF_BYTES // 1000} KB."
        )
    return resp.text


def _pull_target(gh: httpx.Client, repo: str, number: int) -> Target:
    pr = _get(gh, f"/repos/{repo}/pulls/{number}")
    if pr["changed_files"] > MAX_CHANGED_FILES:
        raise ReviewError(
            f"That pull request changes {pr['changed_files']} files; the demo reviews up to {MAX_CHANGED_FILES}."
        )
    return Target(
        repo=repo,
        kind="pull",
        number=number,
        head_sha=pr["head"]["sha"],
        base_sha=pr["base"]["sha"],
        title=pr["title"],
        body=pr.get("body") or "",
        url=pr["html_url"],
        additions=pr["additions"],
        deletions=pr["deletions"],
        changed_files=pr["changed_files"],
        diff=_fetch_diff(f"https://github.com/{repo}/pull/{number}.diff"),
    )


def _commit_target(gh: httpx.Client, repo: str, ref: str) -> Target:
    commit = _get(gh, f"/repos/{repo}/commits/{ref}")
    files = commit.get("files") or []
    if len(files) > MAX_CHANGED_FILES:
        raise ReviewError(f"That commit changes {len(files)} files; the demo reviews up to {MAX_CHANGED_FILES}.")
    message = commit["commit"]["message"]
    title, _, body = message.partition("\n")
    sha = commit["sha"]
    return Target(
        repo=repo,
        kind="commit",
        head_sha=sha,
        base_sha=(commit.get("parents") or [{}])[0].get("sha"),
        title=title,
        body=body.strip(),
        url=commit["html_url"],
        additions=commit.get("stats", {}).get("additions", 0),
        deletions=commit.get("stats", {}).get("deletions", 0),
        changed_files=len(files),
        diff=_fetch_diff(f"https://github.com/{repo}/commit/{sha}.diff"),
    )


def resolve(url: str) -> Target:
    """Turns a repo, pull request or commit link into a reviewable target.

    A bare repository link reviews its most recently updated open pull request that fits the
    demo's limits, or its latest commit when there is none.
    """
    m = GITHUB_URL_RE.match(url.strip())
    if m is None:
        raise ReviewError("Paste a public GitHub link: a repository, a pull request, or a commit.")
    repo = f"{m['owner']}/{m['repo'].removesuffix('.git')}"
    with github_client() as gh:
        info = _get(gh, f"/repos/{repo}")
        if info.get("private"):
            raise ReviewError("That repository is private. The demo reviews public repositories only.")
        if info.get("size", 0) > MAX_REPO_KB:
            raise ReviewError(
                f"That repository is {info['size'] // 1000} MB; the demo handles up to {MAX_REPO_KB // 1000} MB."
            )
        repo = info["full_name"]
        if m["number"]:
            return _pull_target(gh, repo, int(m["number"]))
        if m["sha"]:
            return _commit_target(gh, repo, m["sha"])
        for pr in _get(gh, f"/repos/{repo}/pulls", state="open", sort="updated", direction="desc", per_page=5):
            try:
                return _pull_target(gh, repo, pr["number"])
            except ReviewError:
                continue
        return _commit_target(gh, repo, info["default_branch"])


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def stage(target: Target, workdir: Path) -> Iterator[dict[str, Any]]:
    """Checks out the head commit and copies a size-capped tree into workdir/stage."""
    checkout = workdir / "checkout"
    checkout.mkdir()
    _git("init", "-q", cwd=checkout)
    ref = f"pull/{target.number}/head" if target.kind == "pull" else target.head_sha
    _git(
        "fetch", "-q", "--depth", "1", "--filter=blob:limit=1m", f"https://github.com/{target.repo}", ref, cwd=checkout
    )
    _git("checkout", "-q", "FETCH_HEAD", cwd=checkout)
    yield {"t": "step_detail", "step": "volume", "detail": f"Fetched {target.repo}@{target.head_sha[:7]}"}

    root = workdir / "stage"
    repo_dir = root / "repo"
    repo_dir.mkdir(parents=True)
    staged = skipped = total = 0
    for dirpath, dirnames, filenames in os.walk(checkout):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            src = Path(dirpath) / name
            if src.is_symlink():
                skipped += 1
                continue
            size = src.stat().st_size
            if size > MAX_FILE_BYTES or staged >= MAX_STAGED_FILES or total + size > MAX_STAGED_BYTES:
                skipped += 1
                continue
            dst = repo_dir / src.relative_to(checkout)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            staged += 1
            total += size
    (root / "pr.diff").write_text(target.diff)
    (root / "pr.json").write_text(json.dumps(target.meta(), indent=2))
    yield {"t": "stage", "files": staged, "bytes": total, "skipped": skipped, "path": str(root)}


def volume_name(target: Target) -> str:
    digest = hashlib.sha256(f"{target.repo}@{target.head_sha}".encode()).hexdigest()[:16]
    return f"pr-review-{digest}"


def load_volume(target: Target) -> Iterator[dict[str, Any]]:
    """Uploads the staged checkout to a volume and yields the pinned snapshot to mount.

    Volumes are content-addressed by repo and head SHA, so a second review of the same commit
    reuses the existing snapshot instead of uploading again.
    """
    name = volume_name(target)
    t0 = time.monotonic()
    try:
        existing = Volume(name, create_if_missing=False)
        version = existing.latest_version()
        yield {
            "t": "volume",
            "name": name,
            "version": version.id,
            "reused": True,
            "files": None,
            "bytes": None,
            "seconds": round(time.monotonic() - t0, 1),
        }
        return
    except VolumeNotFoundError:
        pass
    with tempfile.TemporaryDirectory(prefix="pr-review-") as tmp:
        stats: dict[str, Any] = {}
        for event in stage(target, Path(tmp)):
            if event["t"] == "stage":
                stats = event
            else:
                yield event
        vol = Volume(name)
        vol.put_dir(stats["path"])
        version = vol.latest_version()
    yield {
        "t": "volume",
        "name": name,
        "version": version.id,
        "reused": False,
        "files": stats["files"],
        "bytes": stats["bytes"],
        "skipped": stats["skipped"],
        "seconds": round(time.monotonic() - t0, 1),
    }


def agent_image() -> Image:
    return (
        Image.debian_slim(python_version="3.12")
        .apt_install(["ripgrep"])
        .pip_install([f"claude-agent-sdk=={AGENT_SDK_VERSION}"])
        .add_local_file(str(HERE / "review_agent.py"), "/opt/review/review_agent.py", strategy="copy")
    )


@dataclass
class RouterConfig:
    base_url: str  # e.g. https://<api host>/v1/router
    environment_id: str
    key_secret: str  # name of the Chalk secret holding the router API key
    model: str

    @property
    def host(self) -> str:
        host = urlsplit(self.base_url).hostname
        if host is None:
            raise ValueError(f"router base URL has no host: {self.base_url!r}")
        return host


def make_sandbox(volume: str, version: int, router: RouterConfig, lifetime_secs: int) -> Sandbox:
    """A gVisor sandbox whose only reachable host is the Chalk router.

    With a network policy in place, egress is an allowlist. add_credentials puts the router
    host on it and sets CHALK_ROUTER_API_KEY to a placeholder; the egress proxy swaps in the
    real key on requests to that host, so the key never exists inside the sandbox.
    """
    return Sandbox(
        image=agent_image(),
        name=f"pr-review-{os.urandom(4).hex()}",
        cpu="1",
        memory="2Gi",
        volumes=[Volume(volume).at_version(version).mount(REVIEW_ROOT)],
        env={
            "ANTHROPIC_BASE_URL": router.base_url,
            "ANTHROPIC_CUSTOM_HEADERS": f"X-Chalk-Env-Id: {router.environment_id}",
            "REVIEW_MODEL": router.model,
            "REVIEW_ROOT": REVIEW_ROOT,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "USE_BUILTIN_RIPGREP": "0",
        },
        network_policy=NetworkPolicy(allowed_hosts=[]),
        security_policy=ContainerSecurityPolicy(kernel_policy=KernelPolicy.RESTRICTED),
        lifetime=f"{lifetime_secs}s",
    ).add_credentials(router.host, Secret.from_chalk_env(router.key_secret))


PROBE = (
    "import urllib.request\n"
    + "try:\n"
    + "    urllib.request.urlopen('https://github.com', timeout=5)\n"
    + "    print('reachable')\n"
    + "except Exception as e:\n"
    + "    print('blocked:', type(e).__name__)\n"
)


def run_review(target: Target, router: RouterConfig, timeout_secs: int) -> Iterator[dict[str, Any]]:
    """Runs every step, yielding events for the viewer."""
    yield {"t": "target", "target": target.meta(), "diff_bytes": len(target.diff)}

    yield {"t": "step", "step": "volume", "status": "running"}
    vol_event: dict[str, Any] = {}
    for event in load_volume(target):
        if event["t"] == "volume":
            vol_event = event
        yield event
    yield {"t": "step", "step": "volume", "status": "done"}

    yield {"t": "step", "step": "sandbox", "status": "running"}
    t0 = time.monotonic()
    sandbox = make_sandbox(vol_event["name"], vol_event["version"], router, timeout_secs + 120)
    sandbox.run()
    try:
        probe = sandbox.exec("python", "-c", PROBE, timeout_secs=20).stdout_text.strip()
        listing = sandbox.exec(
            "bash", "-c", f"ls {REVIEW_ROOT} && find {REVIEW_ROOT}/repo -type f | wc -l", timeout_secs=20
        ).stdout_text.split()
        yield {
            "t": "sandbox",
            "id": sandbox.id,
            "seconds": round(time.monotonic() - t0, 1),
            "mount": f"{vol_event['name']}@v{vol_event['version']} → {REVIEW_ROOT} (read-only snapshot)",
            "mount_listing": listing[:-1],
            "mounted_files": int(listing[-1]) if listing and listing[-1].isdigit() else None,
            "egress_allowlist": [router.host],
            "probe": f"github.com → {probe}",
            "kernel": "gVisor (restricted)",
        }
        yield {"t": "step", "step": "sandbox", "status": "done"}

        yield {"t": "step", "step": "router", "status": "running"}
        yield {
            "t": "router",
            "base_url": router.base_url,
            "model": router.model,
            "credential": f"{router.key_secret} (placeholder in sandbox, swapped at egress)",
        }
        yield {"t": "step", "step": "session", "status": "running"}
        stdout_buf = ""
        router_confirmed = False
        for event in sandbox.exec_stream(
            "python", "/opt/review/review_agent.py", timeout_secs=timeout_secs, workdir=f"{REVIEW_ROOT}/repo"
        ):
            if event.stdout:
                stdout_buf += event.stdout
                while "\n" in stdout_buf:
                    line, stdout_buf = stdout_buf.split("\n", 1)
                    if not line.strip():
                        continue
                    try:
                        agent_event = json.loads(line)
                    except json.JSONDecodeError:
                        yield {"t": "log", "text": line}
                        continue
                    if not router_confirmed and agent_event.get("t") in ("text", "tool_use"):
                        # The first model output proves the router round trip worked.
                        router_confirmed = True
                        yield {"t": "step", "step": "router", "status": "done"}
                    yield agent_event
            if event.stderr:
                yield {"t": "log", "text": event.stderr[-2000:]}
            if event.is_error:
                yield {"t": "error", "message": f"exec error: {event.error_message}"}
            if event.exit_code is not None:
                yield {"t": "exit", "code": event.exit_code}
        yield {"t": "step", "step": "session", "status": "done"}
    finally:
        try:
            sandbox.terminate()
        except Exception:  # noqa: BLE001 - the sandbox lifetime reaps it anyway
            pass
