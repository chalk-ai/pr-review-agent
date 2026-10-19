#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute>=2.13.8"]
# ///
"""Deploy the PR review agent demo (server.py + pipeline.py + index.html) as a Chalk ScalingGroup.

The server creates volumes and review sandboxes with its own workload identity
(`chalk_identity=True`), so no client secret is baked into the image. Review sandboxes get no
identity: their only credential is a placeholder for the router key, swapped in at egress.

Requirements on the target environment:
  - the Chalk router is enabled (`chalk router status`)
  - a secret holds a router API key (`chalk router api-key create`), CHALK_ROUTER_API_KEY by default

Credentials for the deploy itself come from your existing `chalk login` (or the
CHALK_* env vars):
    ./deploy.py
"""

from __future__ import annotations

import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_FILES = ["server.py", "pipeline.py", "review_agent.py", "index.html"]


def main() -> None:
    from chalkcompute import Image, ScalingGroup

    image = (
        Image.debian_slim(python_version="3.12")
        .apt_install(["git"])
        .pip_install(["chalkcompute>=2.13.8", "httpx", "fastapi==0.116.1", "uvicorn[standard]==0.35.0"])
    )
    # The app files total more than the 32 KiB inline `copy` budget, so they ride a volume
    # mounted at /app when the replica starts. Nothing at build time needs them.
    for name in APP_FILES:
        image = image.add_local_file(str(HERE / name), f"/app/{name}", strategy="volume")
    image = image.workdir("/app")

    sg = ScalingGroup(
        image=image,
        name='pr-review-agent',
        cpu="1",
        memory="2Gi",
        env={

            "PORT": "8080",
            "REVIEW_MODEL": 'anthropic/claude-sonnet-5-5',
            "REVIEW_KEY_SECRET": 'CHALK_ROUTER_API_KEY',
            "REVIEW_MAX_CONCURRENT": '2',
            "REVIEW_MAX_PER_HOUR": '20',
        },
        entrypoint=["python", "/app/server.py"],
        port=8080,
        chalk_identity=True,
        # Jobs, the replay cache and the rate limits live in the replica, and each event stream
        # must reach the replica running its review, so this stays at one.
        min_replicas=1,
        max_replicas=1,
    )
    t0 = time.monotonic()
    try:
        sg.deploy(build_timeout=1200.0, ready_timeout=900.0)
    finally:
        print(f"deploy returned after {time.monotonic() - t0:.0f}s")
    print("web_url:", sg.web_url)


if __name__ == "__main__":
    main()
