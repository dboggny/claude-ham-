# Hosted MCP server

Lets Claude search the entries in this repo and submit new ones for review.
Submissions become pull requests, so the PR list is the review queue.

Tools: `list_categories`, `search_entries`, `get_entry`, `submit_entry`.

## Run locally

    pip install -r requirements.txt
    DATA_DIR=./data uvicorn app:app --port 8080

The MCP endpoint is `http://localhost:8080/mcp`. Without `GITHUB_TOKEN`, search works and submissions are disabled.

## Environment

- `KB_REPO`: `owner/name` of the entries repo (default `dboggny/claude-ham-`)
- `KB_BRANCH`: default `main`
- `GITHUB_TOKEN`: fine-grained token limited to this one repo, with Contents and Pull requests set to read and write
- `DATA_DIR`: where the repo clone is kept (default `/data`)
- `ALLOWED_HOSTS`: optional, the public hostname, e.g. `ham-kb.fly.dev`

## Deploy

Any host that runs a Dockerfile works (Fly.io, Railway, Render). Build from this folder, set the environment variables above, and expose port 8080. Check `/health`.

Hams then add `https://YOUR-HOST/mcp` as a custom connector in Claude.

## Safeguards

- Submissions need a callsign-shaped author (self-reported, not verified)
- Size limits, and a per-callsign and overall hourly rate limit
- Emails, phone numbers, and password-like text are flagged on the PR
- Front matter is generated from validated fields, never pasted from input

## Tests

    pip install pytest anyio
    python -m pytest tests
