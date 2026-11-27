"""Turns a submission into a GitHub pull request. The PR is the review queue."""

from __future__ import annotations

import base64
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import date

import httpx
import yaml

from kb import CATEGORIES

CALLSIGN = re.compile(r"^(?=.*\d)(?=.*[A-Z])[A-Z0-9]{3,7}(/[A-Z0-9]{1,4})?$")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")
SECRET = re.compile(r"(?i)(password|passwd|api[_ -]?key|secret|token)\s*[:=]\s*\S+")

MAX_BODY = 20_000
MAX_LIST = 12


class SubmissionError(ValueError):
    """Raised for input problems the caller can fix."""


class RateLimiter:
    def __init__(self, per_callsign: int = 5, total: int = 30, window: int = 3600):
        self.per_callsign, self.total, self.window = per_callsign, total, window
        self._all: deque[float] = deque()
        self._by: dict[str, deque[float]] = {}

    def check(self, callsign: str) -> None:
        now = time.time()
        for q in (self._all, *self._by.values()):
            while q and now - q[0] > self.window:
                q.popleft()
        mine = self._by.setdefault(callsign, deque())
        if len(self._all) >= self.total or len(mine) >= self.per_callsign:
            raise SubmissionError("Too many submissions right now. Try again later.")
        self._all.append(now)
        mine.append(now)


@dataclass
class Submission:
    title: str
    category: str
    author: str
    summary: str
    details: str
    sources: str = ""
    tags: list[str] | None = None
    bands: list[str] | None = None
    modes: list[str] | None = None


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60].strip("-") or "entry"


def clean_list(values: list[str] | None) -> list[str]:
    out = []
    for v in (values or [])[:MAX_LIST]:
        v = re.sub(r"\s+", " ", str(v)).strip()[:40]
        if v:
            out.append(v)
    return out


def validate(sub: Submission) -> Submission:
    sub.author = sub.author.strip().upper()
    if not CALLSIGN.match(sub.author):
        raise SubmissionError(
            "author must be an amateur callsign such as W1AW (letters and digits).")
    if sub.category not in CATEGORIES:
        raise SubmissionError(f"category must be one of: {', '.join(CATEGORIES)}")
    sub.title = re.sub(r"\s+", " ", sub.title).strip()
    if not 5 <= len(sub.title) <= 120:
        raise SubmissionError("title must be 5 to 120 characters.")
    if len(sub.summary.strip()) < 10 or len(sub.details.strip()) < 30:
        raise SubmissionError("summary and details are too short to be useful.")
    if len(sub.summary) + len(sub.details) + len(sub.sources) > MAX_BODY:
        raise SubmissionError(f"entry is too long (max {MAX_BODY} characters).")
    sub.tags, sub.bands, sub.modes = (
        clean_list(sub.tags), clean_list(sub.bands), clean_list(sub.modes))
    return sub


def privacy_flags(sub: Submission) -> list[str]:
    text = "\n".join([sub.summary, sub.details, sub.sources])
    flags = []
    if EMAIL.search(text):
        flags.append("contains an email address")
    if PHONE.search(text):
        flags.append("contains a phone number")
    if SECRET.search(text):
        flags.append("may contain a password, key, or token")
    return flags


def render(sub: Submission) -> str:
    meta = {
        "title": sub.title,
        "category": sub.category,
        "author": sub.author,
        "tags": sub.tags,
        "bands": sub.bands,
        "modes": sub.modes,
        "created": date.today().isoformat(),
    }
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True,
                           default_flow_style=None).strip()
    parts = [f"---\n{front}\n---\n", f"## Summary\n{sub.summary.strip()}\n",
             f"## Details\n{sub.details.strip()}\n"]
    if sub.sources.strip():
        parts.append(f"## Sources\n{sub.sources.strip()}\n")
    return "\n".join(parts)


class GitHubSubmitter:
    def __init__(self, repo: str, base_branch: str, token: str,
                 client: httpx.Client | None = None):
        self.repo, self.base = repo, base_branch
        self.http = client or httpx.Client(
            base_url="https://api.github.com", timeout=30,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})

    def _call(self, method: str, path: str, **kw) -> dict:
        r = self.http.request(method, f"/repos/{self.repo}/{path}", **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {method} {path} failed: {r.status_code}")
        return r.json()

    def _exists(self, path: str) -> bool:
        r = self.http.get(f"/repos/{self.repo}/contents/{path}",
                          params={"ref": self.base})
        return r.status_code == 200

    def open_pr(self, sub: Submission) -> str:
        slug = slugify(sub.title)
        path = f"entries/{sub.category}/{slug}.md"
        n = 2
        while self._exists(path):
            path = f"entries/{sub.category}/{slug}-{n}.md"
            n += 1
        branch = f"submission/{sub.category}-{slug}-{int(time.time())}"

        base_sha = self._call("GET", f"git/ref/heads/{self.base}")["object"]["sha"]
        self._call("POST", "git/refs",
                   json={"ref": f"refs/heads/{branch}", "sha": base_sha})
        self._call("PUT", f"contents/{path}", json={
            "message": f"Add entry: {sub.title}",
            "content": base64.b64encode(render(sub).encode()).decode(),
            "branch": branch})

        flags = privacy_flags(sub)
        body = (f"Submitted through the hosted MCP server by **{sub.author}**.\n\n"
                f"Category: `{sub.category}`\n\n")
        body += ("Automated flags, please check before merging:\n"
                 + "".join(f"- {f}\n" for f in flags)
                 if flags else "No automated privacy flags.\n")
        body += ("\nThe callsign is self-reported and not verified. "
                 "Review the content before merging.")
        pr = self._call("POST", "pulls", json={
            "title": f"Add entry: {sub.title}", "head": branch,
            "base": self.base, "body": body})
        return pr["html_url"]
