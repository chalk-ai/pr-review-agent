# PR review agent on Chalk

Paste a public GitHub link and watch an AI reviewer work through the pull request live. The
review ends with comments shaped exactly the way GitHub's
[create-review API](https://docs.github.com/en/rest/pulls/reviews#create-a-review-for-a-pull-request)
expects.

The demo is mostly about where the agent runs:

1. **Volume.** The server fetches the pull request's head commit and diff, then writes them to a
   Chalk volume. The sandbox mounts a pinned version, an immutable read-only snapshot, at
   `/review`.
2. **No-egress container.** The [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
   runs in a gVisor sandbox with an empty network allowlist: no egress and no DNS. A probe to
   github.com fails before the agent starts.
3. **Chalk router.** The one host the sandbox can reach is the Chalk router, which serves the
   Anthropic Messages API that the Agent SDK speaks. The key inside the sandbox is a placeholder,
   and the egress proxy swaps in the real router key on requests to that host.
4. **Live session.** The agent streams tokens and tool calls as JSON lines. The server relays them
   to the browser as server-sent events.
5. **Feedback objects.** Comments arrive through two custom tools, `add_review_comment` and
   `submit_review`. They reject lines outside the diff, so every comment is valid for GitHub,
   and the run ends with a complete review payload.

## Files

| File | Role |
| --- | --- |
| `pipeline.py` | GitHub link → volume → sandbox → agent. Each step yields events. |
| `review_agent.py` | Runs inside the sandbox: Claude Agent SDK, read-only tools, the review tools. |
| `server.py` | FastAPI app: the page, a job per review, SSE, replay cache, rate limits. |
| `index.html` | The demo page. `?embed=1` hides its header for embedding in an iframe. |
| `review_cli.py` | The same pipeline from a terminal. |
| `deploy.py` | Deploys the server as a `chalkcompute.ScalingGroup`. |
| `test_review_agent.py` | Tests for diff-line parsing and path normalization. |

## Run it

You need [`uv`](https://docs.astral.sh/uv/) and a Chalk environment with the router enabled
(`chalk router status`). Credentials come from the SDK’s ambient credentials from `chalk login`.

```bash
chalk login

# One time: a router key for the sandbox. Paste the key `chalk router api-key create` prints.
chalk secrets set CHALK_ROUTER_API_KEY

# Review from the terminal...
./review_cli.py https://github.com/pallets/markupsafe/pull/528

# ...or serve the web demo from a scaling group, and open the printed URL.
./deploy.py
```

`review_cli.py` also accepts a bare repository link, which reviews its most recently updated open
pull request (or its latest commit when there is none), and a commit link.

## What a visitor can and can't do

- **Sandboxes:**
  - a restricted kernel (gVisor)
  - no network except the router host
  - no Chalk identity
  - a placeholder instead of the key
  - read-only input
  - a hard `lifetime`
  - terminated when the review ends
- **Agent tools:** only `Read`, `Grep`, `Glob` and the two review tools. It can't run commands,
  write files or browse.
- **Size limits** (`pipeline.py`): repositories up to 400 MB, diffs up to 300 KB, and up to 120
  changed files. The staged checkout is capped by file count and total size, and skips symlinks
  and vendored directories.
- **Server limits** (`server.py`): two concurrent fresh reviews and 20 fresh reviews per hour.
  - Finished reviews are cached by repository and head SHA on a Chalk volume.
  - A repeat request replays the recorded run instead of starting a new agent session.
  - The featured examples are pre-run at startup, so visitors see them instantly.
- **Server credentials:** the server creates volumes and sandboxes with its own workload
  identity (`chalk_identity=True`), so no client secret is in the image.

## Tests

```bash
uv run --with pytest --with claude-agent-sdk python -m pytest test_review_agent.py
```

## License

Apache-2.0.

## Local authentication

The SDK reads credentials, API host, and the selected environment from your existing
`chalk login` in `~/.chalk.yml`, including project-scoped and default logins.
For unattended runs, `CHALK_CLIENT_ID` and `CHALK_CLIENT_SECRET` remain supported. Where available, `--env-id`
overrides the target environment; otherwise the SDK uses your login or environment settings.
