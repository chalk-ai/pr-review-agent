"""PR review agent that runs inside a Chalk sandbox.

The pull request arrives on a read-only volume mounted at REVIEW_ROOT:

    REVIEW_ROOT/pr.json    metadata (repo, number, title, body, base and head SHAs)
    REVIEW_ROOT/pr.diff    the unified diff under review
    REVIEW_ROOT/repo/      a checkout of the head commit

The Claude Agent SDK reads the diff and the code around it with read-only tools, then files
feedback through two custom tools. Every event is printed to stdout as one JSON object per
line, which the server relays to the browser as it happens:

    {"t": "text", "text": ...}                     streamed assistant text
    {"t": "tool_use", "id", "name", "input"}       a tool call
    {"t": "tool_result", "id", "is_error", "preview"}
    {"t": "comment", "comment": {...}}             one review comment, GitHub-shaped
    {"t": "review", "review": {...}}               the pull-request review payload
    {"t": "result", "turns", "duration_ms", "cost_usd", "usage"}
    {"t": "error", "message"}
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

REVIEW_ROOT = Path(os.environ.get("REVIEW_ROOT", "/review"))
MODEL = os.environ.get("REVIEW_MODEL", "anthropic/claude-sonnet-5-5")
MAX_TURNS = int(os.environ.get("REVIEW_MAX_TURNS", "40"))
MAX_COMMENTS = int(os.environ.get("REVIEW_MAX_COMMENTS", "10"))
SEVERITIES = ["critical", "major", "minor", "nit"]
CATEGORIES = ["bug", "security", "performance", "reliability", "maintainability", "testing", "docs"]
REVIEW_EVENTS = ["COMMENT", "REQUEST_CHANGES", "APPROVE"]
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def commentable_lines(diff: str) -> dict[str, dict[str, set[int]]]:
    """Maps each file to the lines GitHub accepts review comments on: RIGHT (new) and LEFT (old)."""
    files: dict[str, dict[str, set[int]]] = {}
    current: dict[str, set[int]] | None = None
    old_line = new_line = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                current = None
                continue
            current = files.setdefault(path.removeprefix("b/"), {"RIGHT": set(), "LEFT": set()})
        elif line.startswith("--- "):
            continue
        elif (m := HUNK_RE.match(line)) is not None:
            old_line, new_line = int(m.group(1)), int(m.group(3))
        elif current is None or line.startswith("diff --git"):
            continue
        elif line.startswith("+"):
            current["RIGHT"].add(new_line)
            new_line += 1
        elif line.startswith("-"):
            current["LEFT"].add(old_line)
            old_line += 1
        elif line.startswith(" "):
            # Context lines are commentable on either side.
            current["RIGHT"].add(new_line)
            current["LEFT"].add(old_line)
            old_line += 1
            new_line += 1
    return files


def normalize_path(path: str, known: dict[str, Any]) -> str:
    """Accepts the absolute, repo/- or git a/b/-prefixed forms the model sometimes uses for a repo path."""
    path = path.strip()
    for prefix in (f"{REVIEW_ROOT}/repo/", "repo/", "./"):
        path = path.removeprefix(prefix)
    path = path.lstrip("/")
    # Strip a git-style a/ or b/ only when that turns an unknown path into a file in the diff.
    if path not in known and path[:2] in ("a/", "b/") and path[2:] in known:
        path = path[2:]
    return path


def tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def tool_ok(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


def build_review_server(meta: dict[str, Any], lines: dict[str, dict[str, set[int]]]) -> Any:
    comments: list[dict[str, Any]] = []

    @tool(
        "add_review_comment",
        "File one review comment anchored to a line of the diff. GitHub rejects comments on lines "
        + "outside the diff, so `line` must fall inside a hunk: side RIGHT for added or context "
        + "lines in the new file, LEFT for deleted lines.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the repo root, as it appears in the diff, e.g. src/app.py.",
                },
                "line": {"type": "integer"},
                "side": {"type": "string", "enum": ["RIGHT", "LEFT"]},
                "severity": {"type": "string", "enum": SEVERITIES},
                "category": {"type": "string", "enum": CATEGORIES},
                "title": {"type": "string", "description": "One-line summary, under 80 chars."},
                "body": {"type": "string", "description": "Markdown: what is wrong and why."},
                "suggestion": {
                    "type": "string",
                    "description": "Optional replacement for the commented line(s), no fences.",
                },
            },
            "required": ["path", "line", "side", "severity", "category", "title", "body"],
        },
    )
    async def add_review_comment(args: dict[str, Any]) -> dict[str, Any]:
        if len(comments) >= MAX_COMMENTS:
            return tool_error(f"Comment limit ({MAX_COMMENTS}) reached. Call submit_review now.")
        path, line, side = normalize_path(args["path"], lines), int(args["line"]), args["side"]
        if path not in lines:
            return tool_error(f"{path} is not in this diff. Files in the diff: {', '.join(sorted(lines))}")
        if line not in lines[path][side]:
            valid = sorted(lines[path][side])
            sample = ", ".join(str(n) for n in valid[:40])
            return tool_error(f"{path}:{line} ({side}) is outside the diff. Commentable {side} lines include: {sample}")
        body = f"**{args['severity'].upper()} · {args['category']}** — {args['title']}\n\n{args['body']}"
        if args.get("suggestion"):
            body += f"\n\n```suggestion\n{args['suggestion'].rstrip()}\n```"
        comment = {
            # Exactly the shape of one entry in `comments` for POST /repos/{owner}/{repo}/pulls/{n}/reviews.
            "github": {"path": path, "line": line, "side": side, "body": body},
            "severity": args["severity"],
            "category": args["category"],
            "title": args["title"],
        }
        comments.append(comment)
        emit({"t": "comment", "comment": comment})
        return tool_ok(f"Comment {len(comments)} recorded on {path}:{line}.")

    @tool(
        "submit_review",
        "Submit the review once all comments are filed. Call exactly once, last.",
        {
            "type": "object",
            "properties": {
                "event": {"type": "string", "enum": REVIEW_EVENTS},
                "summary": {"type": "string", "description": "Markdown review summary."},
            },
            "required": ["event", "summary"],
        },
    )
    async def submit_review(args: dict[str, Any]) -> dict[str, Any]:
        review = {
            "endpoint": f"POST /repos/{meta['repo']}/pulls/{meta['number']}/reviews",
            "payload": {
                "commit_id": meta["head_sha"],
                "event": args["event"],
                "body": args["summary"],
                "comments": [c["github"] for c in comments],
            },
        }
        emit({"t": "review", "review": review})
        return tool_ok("Review submitted. You are done.")

    return create_sdk_mcp_server(name="review", version="1.0.0", tools=[add_review_comment, submit_review])


def system_prompt(meta: dict[str, Any], diff_files: list[str]) -> str:
    kind = "pull request" if meta.get("kind") == "pull" else "commit"
    return f"""You are a senior engineer reviewing a {kind} in {meta["repo"]}.

Title: {meta.get("title", "")}
Description:
{(meta.get("body") or "(none)")[:4000]}

The diff is at {REVIEW_ROOT}/pr.diff and a checkout of the head commit is your working
directory. Files changed: {", ".join(diff_files[:60])}{" …" if len(diff_files) > 60 else ""}.

How to work:
1. Read {REVIEW_ROOT}/pr.diff first.
2. Open the surrounding code with Read, Grep and Glob wherever the diff alone is not enough to
   judge correctness: callers, definitions, tests.
3. For each concrete, actionable problem, call add_review_comment. Prefer real bugs, security
   issues, broken edge cases, races, missing error handling and missing tests over style. Skip
   anything a linter would catch. Two to eight high-signal comments beat a long list. Include a
   `suggestion` when a small replacement fixes the problem.
4. Finish with exactly one submit_review call. Use REQUEST_CHANGES only for critical or major
   problems, APPROVE if you found nothing material, otherwise COMMENT.

Narrate briefly as you go (one or two sentences between tool calls) so a reader can follow
your reasoning. You cannot run code or reach the network."""


async def run() -> int:
    meta = json.loads((REVIEW_ROOT / "pr.json").read_text())
    diff = (REVIEW_ROOT / "pr.diff").read_text(errors="replace")
    lines = commentable_lines(diff)

    # The egress proxy replaces this placeholder with the real router key on the way out.
    os.environ["ANTHROPIC_AUTH_TOKEN"] = os.environ["CHALK_ROUTER_API_KEY"]

    options = ClaudeAgentOptions(
        model=MODEL,
        cwd=str(REVIEW_ROOT / "repo"),
        add_dirs=[str(REVIEW_ROOT)],
        system_prompt=system_prompt(meta, sorted(lines)),
        tools=["Read", "Grep", "Glob"],
        allowed_tools=["Read", "Grep", "Glob", "mcp__review__add_review_comment", "mcp__review__submit_review"],
        mcp_servers={"review": build_review_server(meta, lines)},
        max_turns=MAX_TURNS,
        include_partial_messages=True,
        setting_sources=[],
    )
    prompt = f"Review {meta['repo']} {meta.get('label', '')}. Start by reading the diff."
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, StreamEvent):
            delta = message.event.get("delta", {})
            if message.event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                emit({"t": "text", "text": delta["text"]})
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    emit({"t": "tool_use", "id": block.id, "name": block.name, "input": block.input})
                elif isinstance(block, TextBlock):
                    emit({"t": "text_end"})
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    content = block.content
                    if isinstance(content, list):
                        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                    text = str(content or "")
                    emit(
                        {
                            "t": "tool_result",
                            "id": block.tool_use_id,
                            "is_error": bool(block.is_error),
                            "preview": text[:600],
                            "lines": text.count("\n") + 1 if text else 0,
                        }
                    )
        elif isinstance(message, ResultMessage):
            emit(
                {
                    "t": "result",
                    "turns": message.num_turns,
                    "duration_ms": message.duration_ms,
                    "cost_usd": message.total_cost_usd,
                    "usage": message.usage,
                    "is_error": message.is_error,
                }
            )
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(run()))
    except Exception as exc:  # noqa: BLE001 - reported to the viewer, then exit nonzero
        emit({"t": "error", "message": f"{type(exc).__name__}: {exc}"})
        sys.exit(1)


if __name__ == "__main__":
    main()
