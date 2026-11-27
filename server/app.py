"""Hosted MCP server for sharing ham radio knowledge.

Run:  uvicorn app:app --host 0.0.0.0 --port $PORT
Env:  KB_REPO (owner/name), KB_BRANCH, GITHUB_TOKEN, DATA_DIR,
      ALLOWED_HOSTS (comma list, e.g. ham-kb.fly.dev; optional)
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from kb import CATEGORIES, KnowledgeBase
from submit import (GitHubSubmitter, RateLimiter, Submission, SubmissionError,
                    validate)

INSTRUCTIONS = """Shared ham radio knowledge base built by hams using Claude.
Use search_entries before answering ham radio questions about antennas, code plugs,
digital modes, software, and similar topics, and cite the entry id and author callsign.
When a user wants to share their work: write the entry from what they did, ask for their
callsign, remove private details (home address, phone, email, passwords, license ID),
show the draft, and call submit_entry only after they confirm. Submissions go to a human
review queue and are not live until approved."""


def build_server(kb: KnowledgeBase, submitter: GitHubSubmitter | None,
                 limiter: RateLimiter) -> MCPServer:
    mcp = MCPServer("ham-knowledge", instructions=INSTRUCTIONS)

    @mcp.tool()
    def list_categories() -> dict:
        """List entry categories and how many entries each has."""
        return kb.counts()

    @mcp.tool()
    def search_entries(query: str = "", category: str | None = None,
                       band: str | None = None, mode: str | None = None,
                       limit: int = 8) -> list[dict]:
        """Search shared entries. Filter by category, band (e.g. 40m) or mode (e.g. FT8)."""
        if category and category not in CATEGORIES:
            raise ToolError(f"category must be one of: {', '.join(CATEGORIES)}")
        return [e.summary() for e in kb.search(query, category, band, mode, limit)]

    @mcp.tool()
    def get_entry(entry_id: str) -> dict:
        """Get the full text of an entry by id, like 'antennas/40m-efhw-build'."""
        entry = kb.get(entry_id)
        if entry is None:
            raise ToolError(f"No entry with id {entry_id!r}. Use search_entries.")
        return {**entry.summary(), "created": entry.created, "body": entry.body}

    @mcp.tool()
    def submit_entry(title: str, category: str, author_callsign: str, summary: str,
                     details: str, sources: str = "", tags: list[str] | None = None,
                     bands: list[str] | None = None,
                     modes: list[str] | None = None) -> dict:
        """Submit an entry for review. Only call after the user confirmed the draft.
        Must not contain private data. It becomes public once a maintainer approves it."""
        if submitter is None:
            raise ToolError("Submissions are not enabled on this server.")
        try:
            sub = validate(Submission(title, category, author_callsign, summary,
                                      details, sources, tags, bands, modes))
            limiter.check(sub.author)
        except SubmissionError as err:
            raise ToolError(str(err)) from None
        try:
            url = submitter.open_pr(sub)
        except (RuntimeError, httpx.HTTPError):
            raise ToolError("Could not file the submission right now. "
                             "Try again later.") from None
        return {"status": "submitted for review", "review_url": url}

    return mcp


def build_app(mcp: MCPServer, allowed_hosts: list[str] | None = None) -> Starlette:
    # The SDK defaults to localhost-only host checks, which would reject the
    # public hostname. Check against ALLOWED_HOSTS when given, else skip the
    # check (the server is stateless, cookie-free and public by design).
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed_hosts),
        allowed_hosts=allowed_hosts or [])
    asgi = mcp.streamable_http_app(stateless_http=True, transport_security=security)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    async def health(_request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/health", health), Mount("/", app=asgi)],
                     lifespan=lifespan)


def from_env() -> Starlette:
    repo = os.environ.get("KB_REPO", "dboggny/claude-ham-")
    branch = os.environ.get("KB_BRANCH", "main")
    token = os.environ.get("GITHUB_TOKEN")
    kb = KnowledgeBase(f"https://github.com/{repo}", branch,
                       Path(os.environ.get("DATA_DIR", "/data")) / "kb")
    submitter = GitHubSubmitter(repo, branch, token) if token else None
    hosts = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
    return build_app(build_server(kb, submitter, RateLimiter()), hosts)


# Created lazily so importing this module (tests) needs no environment.
def __getattr__(name: str):
    if name == "app":
        return from_env()
    raise AttributeError(name)
