#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute>=2.13.8", "httpx"]
# ///
"""Review a public GitHub pull request from the command line, using the same pipeline as the web demo.

    ./review_cli.py https://github.com/<owner>/<repo>/pull/<n>

Credentials come from your existing `chalk login` (or the CHALK_* env vars). The
environment needs the Chalk router enabled and a secret holding a router API key
(`chalk router api-key create`, stored as CHALK_ROUTER_API_KEY by default).
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    args = parser.parse_args()

    from pipeline import ReviewError, RouterConfig, resolve, run_review

    from chalkcompute import ConnectClient

    with ConnectClient() as connection:
        router = RouterConfig(
            base_url=connection.api_server + "/v1/router",
            environment_id=connection.environment_id,
            key_secret='CHALK_ROUTER_API_KEY',
            model='anthropic/claude-sonnet-5-5',
        )
    try:
        target = resolve(args.url)
    except ReviewError as exc:
        sys.exit(str(exc))

    for event in run_review(target, router, 600):
        t = event["t"]
        if t == "text":
            print(event["text"], end="", flush=True)
        elif t == "text_end":
            print(flush=True)
        elif t == "tool_use":
            print(f"\n  → {event['name']} {json.dumps(event['input'])[:160]}", flush=True)
        elif t == "tool_result":
            status = "error" if event["is_error"] else f"{event['lines']} lines"
            print(f"  ← {status}", flush=True)
        elif t == "comment":
            c = event["comment"]
            print(f"\n  ✎ [{c['severity']}] {c['github']['path']}:{c['github']['line']} {c['title']}", flush=True)
        elif t in ("step", "step_detail"):
            continue
        else:
            print(f"\n[{t}] {json.dumps({k: v for k, v in event.items() if k != 't'})[:1500]}", flush=True)


if __name__ == "__main__":
    main()
