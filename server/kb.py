"""Read-only access to the knowledge base: a git clone of the entries repo."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CATEGORIES = [
    "antennas",
    "code-plugs",
    "software",
    "digital-modes",
    "operating",
    "repeaters",
    "contesting",
    "satellites",
    "emcomm",
    "build-projects",
]

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_WORD = re.compile(r"[a-z0-9][a-z0-9+\-./]*")


@dataclass
class Entry:
    id: str  # "category/slug"
    title: str
    category: str
    author: str
    tags: list[str] = field(default_factory=list)
    bands: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    created: str = ""
    body: str = ""

    def summary(self) -> dict:
        first = next((ln.strip() for ln in self.body.splitlines()
                      if ln.strip() and not ln.startswith("#")), "")
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "author": self.author,
            "tags": self.tags,
            "bands": self.bands,
            "modes": self.modes,
            "snippet": first[:240],
        }


def parse_entry(path: Path, root: Path) -> Entry | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = _FRONTMATTER.match(text)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict) or not meta.get("title"):
        return None
    rel = path.relative_to(root)
    category = rel.parts[0]
    as_list = lambda v: [str(x) for x in v] if isinstance(v, list) else []
    return Entry(
        id=f"{category}/{path.stem}",
        title=str(meta["title"]),
        category=category,
        author=str(meta.get("author", "")),
        tags=as_list(meta.get("tags")),
        bands=as_list(meta.get("bands")),
        modes=as_list(meta.get("modes")),
        created=str(meta.get("created", "")),
        body=m.group(2).strip(),
    )


class KnowledgeBase:
    """Keeps a shallow git clone fresh and searches the entries in it."""

    def __init__(self, repo_url: str, branch: str, data_dir: Path,
                 refresh_seconds: int = 300):
        self.repo_url = repo_url
        self.branch = branch
        self.dir = data_dir
        self.refresh_seconds = refresh_seconds
        self._entries: list[Entry] = []
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    # -- git ------------------------------------------------------------
    def _git(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                       text=True, timeout=120)

    def _sync(self) -> None:
        if (self.dir / ".git").exists():
            self._git("fetch", "--depth=1", "origin", self.branch, cwd=self.dir)
            self._git("reset", "--hard", "FETCH_HEAD", cwd=self.dir)
        else:
            self.dir.parent.mkdir(parents=True, exist_ok=True)
            self._git("clone", "--depth=1", "--branch", self.branch,
                      self.repo_url, str(self.dir))

    # -- loading --------------------------------------------------------
    def _load(self) -> list[Entry]:
        root = self.dir / "entries"
        entries = []
        if root.exists():
            for path in sorted(root.glob("*/*.md")):
                if path.name.startswith("_") or path.parent.name not in CATEGORIES:
                    continue
                entry = parse_entry(path, root)
                if entry:
                    entries.append(entry)
        return entries

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            if not force and time.time() - self._loaded_at < self.refresh_seconds:
                return
            try:
                self._sync()
            except (subprocess.SubprocessError, OSError):
                if not self._entries and not (self.dir / ".git").exists():
                    raise
            self._entries = self._load()
            self._loaded_at = time.time()

    @property
    def entries(self) -> list[Entry]:
        self.refresh()
        return self._entries

    # -- queries --------------------------------------------------------
    def get(self, entry_id: str) -> Entry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    def counts(self) -> dict[str, int]:
        counts = {c: 0 for c in CATEGORIES}
        for e in self.entries:
            counts[e.category] = counts.get(e.category, 0) + 1
        return counts

    def search(self, query: str = "", category: str | None = None,
               band: str | None = None, mode: str | None = None,
               limit: int = 8) -> list[Entry]:
        terms = _WORD.findall(query.lower())
        scored = []
        for e in self.entries:
            if category and e.category != category:
                continue
            if band and band.lower() not in (b.lower() for b in e.bands):
                continue
            if mode and mode.lower() not in (m.lower() for m in e.modes):
                continue
            if not terms:
                scored.append((0, e))
                continue
            title = e.title.lower()
            tags = " ".join(e.tags).lower()
            body = e.body.lower()
            score = 0
            for t in terms:
                score += 5 * (t in title) + 3 * (t in tags) + min(body.count(t), 5)
            if score:
                scored.append((score, e))
        scored.sort(key=lambda s: (-s[0], s[1].title))
        return [e for _, e in scored[: max(1, min(limit, 25))]]
