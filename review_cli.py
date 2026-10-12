#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute==2.13.4", "httpx"]
# ///
"""Review a public GitHub pull request from the command line, using the same pipeline as the web demo.

    ./review_cli.py https://github.com/<owner>/<repo>/pull/<n> [--env-id <environment>]

Credentials come from `chalk config` in the current directory (or the CHALK_* env vars). The
environment needs the Chalk router enabled and a secret holding a router API key
(`chalk router api-key create`, stored as CHALK_ROUTER_API_KEY by default).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def load_chalk_env(env_id: str | None) -> None:
    if "CHALK_CLIENT_ID" not in os.environ:
        cfg = json.loads(subprocess.check_output(["chalk", "config", "--json"]))
        os.environ["CHALK_API_SERVER"] = cfg["apiServer"]["value"]
        os.environ["CHALK_CLIENT_ID"] = cfg["clientId"]["value"]
        os.environ["CHALK_CLIENT_SECRET"] = cfg["clientSecret"]["value"]
        os.environ.setdefault("CHALK_ENVIRONMENT_ID", cfg["environment"]["value"])
    if env_id:
        os.environ["CHALK_ENVIRONMENT_ID"] = env_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--env-id", default=None)
    parser.add_argument("--model", default="anthropic/claude-sonnet-5-5")
    parser.add_argument("--key-secret", default="CHALK_ROUTER_API_KEY")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    load_chalk_env(args.env_id)
    from pipeline import ReviewError, RouterConfig, resolve, run_review

    router = RouterConfig(
        base_url=os.environ["CHALK_API_SERVER"].rstrip("/") + "/v1/router",
        environment_id=os.environ["CHALK_ENVIRONMENT_ID"],
        key_secret=args.key_secret,
        model=args.model,
    )
    try:
        target = resolve(args.url)
    except ReviewError as exc:
        sys.exit(str(exc))

    for event in run_review(target, router, args.timeout):
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
