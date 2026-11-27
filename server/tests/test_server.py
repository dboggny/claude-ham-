import base64
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import build_app, build_server  # noqa: E402
from kb import KnowledgeBase, parse_entry  # noqa: E402
from submit import (GitHubSubmitter, RateLimiter, Submission,  # noqa: E402
                    SubmissionError, privacy_flags, render, slugify, validate)

ENTRY = """---
title: 40m EFHW build notes
category: antennas
author: W1AW
tags: [efhw, wire]
bands: [40m]
modes: [SSB, CW]
created: 2026-11-26
---

## Summary
End-fed half wave for 40m with a 49:1 transformer.

## Details
Use 20 turns on an FT140-43 core.
"""


@pytest.fixture
def origin(tmp_path):
    """A local git repo standing in for the GitHub repo."""
    repo = tmp_path / "origin"
    (repo / "entries" / "antennas").mkdir(parents=True)
    (repo / "entries" / "antennas" / "40m-efhw.md").write_text(ENTRY)
    (repo / "entries" / "antennas" / "_template.md").write_text(ENTRY)
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True,
                                    capture_output=True)
    run("init", "-q", "-b", "main")
    run("-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return repo


@pytest.fixture
def kb(origin, tmp_path):
    return KnowledgeBase(origin.as_uri(), "main", tmp_path / "clone")


def test_loads_and_skips_template(kb):
    assert [e.id for e in kb.entries] == ["antennas/40m-efhw"]


def test_search_ranks_and_filters(kb):
    assert kb.search("efhw")[0].id == "antennas/40m-efhw"
    assert kb.search("efhw", band="20m") == []
    assert kb.search("efhw", mode="cw")
    assert kb.search("nothing-matches-this") == []
    assert kb.counts()["antennas"] == 1


def test_get_and_missing(kb):
    assert "49:1" in kb.get("antennas/40m-efhw").body
    assert kb.get("antennas/nope") is None


def test_refresh_picks_up_new_commits(kb, origin):
    kb.refresh(force=True)
    (origin / "entries" / "antennas" / "second.md").write_text(
        ENTRY.replace("40m EFHW build notes", "Second"))
    for a in (("add", "."), ("commit", "-qm", "two")):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                       cwd=origin, check=True, capture_output=True)
    kb.refresh(force=True)
    assert len(kb.entries) == 2


def good(**kw):
    base = dict(title="Dipole for 20m", category="antennas", author="k2abc",
                summary="Simple 20m dipole build.",
                details="Cut 33 feet of wire per leg and hang it as an inverted V.")
    base.update(kw)
    return Submission(**base)


def test_validation():
    assert validate(good()).author == "K2ABC"
    for bad in (dict(author="hello"), dict(author="1234"), dict(category="nope"),
                dict(title="x"), dict(details="short")):
        with pytest.raises(SubmissionError):
            validate(good(**bad))


def test_frontmatter_injection_is_neutralised(tmp_path):
    sub = validate(good(title="Evil\n---\nauthor: X", tags=["a: b", "c\nd"]))
    p = tmp_path / "x.md"
    p.write_text(render(sub))
    (tmp_path / "antennas").mkdir()
    (tmp_path / "antennas" / "x.md").write_text(render(sub))
    entry = parse_entry(tmp_path / "antennas" / "x.md", tmp_path)
    assert entry.author == "K2ABC"
    assert "\n" not in entry.title


def test_privacy_flags():
    s = good(details="Email me at a@b.com or 212-555-1234, password: hunter2 ok")
    assert len(privacy_flags(s)) == 3
    assert privacy_flags(good()) == []


def test_slug():
    assert slugify("My  Cool/Antenna!!") == "my-cool-antenna"
    assert slugify("!!!") == "entry"


def fake_github():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        p = req.url.path
        if req.method == "GET" and "/contents/" in p:
            return httpx.Response(404)
        if req.method == "GET" and "/git/ref/heads/main" in p:
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if req.method == "POST" and p.endswith("/git/refs"):
            return httpx.Response(201, json={})
        if req.method == "PUT" and "/contents/" in p:
            body = req.read()
            calls.append(("BODY", body))
            return httpx.Response(201, json={})
        if req.method == "POST" and p.endswith("/pulls"):
            return httpx.Response(201, json={"html_url": "https://github.com/x/y/pull/7"})
        return httpx.Response(500)

    client = httpx.Client(base_url="https://api.github.com",
                          transport=httpx.MockTransport(handler))
    return GitHubSubmitter("x/y", "main", "tok", client), calls


def test_open_pr_flow():
    sub, calls = fake_github()
    url = sub.open_pr(validate(good()))
    assert url.endswith("/pull/7")
    put = next(c[1] for c in calls if c[0] == "BODY")
    import json
    content = base64.b64decode(json.loads(put)["content"]).decode()
    assert yaml.safe_load(content.split("---")[1])["author"] == "K2ABC"
    assert [m for m, _ in calls if m in ("POST", "PUT")] == ["POST", "PUT", "POST"]


def test_rate_limit():
    rl = RateLimiter(per_callsign=2, total=10)
    rl.check("K2ABC")
    rl.check("K2ABC")
    with pytest.raises(SubmissionError):
        rl.check("K2ABC")
    rl.check("W1AW")


@pytest.mark.anyio
async def test_mcp_end_to_end(kb):
    from starlette.testclient import TestClient
    submitter, _ = fake_github()
    app = build_app(build_server(kb, submitter, RateLimiter()))
    hdr = {"Accept": "application/json, text/event-stream",
           "Content-Type": "application/json"}

    def rpc(c, method, params=None, id=1):
        r = c.post("/mcp", headers=hdr, json={"jsonrpc": "2.0", "id": id,
                                               "method": method,
                                               "params": params or {}})
        assert r.status_code == 200, r.text
        return r.text

    with TestClient(app) as c:
        assert c.get("/health").text == "ok"
        init = rpc(c, "initialize", {"protocolVersion": "2025-06-18",
                                     "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}})
        assert "ham-knowledge" in init
        tools = rpc(c, "tools/list")
        for name in ("search_entries", "get_entry", "list_categories", "submit_entry"):
            assert name in tools
        found = rpc(c, "tools/call", {"name": "search_entries",
                                      "arguments": {"query": "efhw"}})
        assert "antennas/40m-efhw" in found
        sub = rpc(c, "tools/call", {"name": "submit_entry", "arguments": {
            "title": "Dipole for 20m", "category": "antennas",
            "author_callsign": "K2ABC", "summary": "Simple 20m dipole build.",
            "details": "Cut 33 feet of wire per leg and hang it as an inverted V."}})
        assert "pull/7" in sub
        bad = rpc(c, "tools/call", {"name": "submit_entry", "arguments": {
            "title": "Dipole for 20m", "category": "antennas",
            "author_callsign": "nope", "summary": "Simple 20m dipole build.",
            "details": "Cut 33 feet of wire per leg and hang it as an inverted V."}})
        assert "callsign" in bad
