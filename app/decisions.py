"""Section 8 — render each repo's DECISIONS.md inside the app.

Reads from a read-only bind mount of the repo checkouts. Two rules matter
here, both about not trusting the filesystem more than necessary:

  * Only files literally named DECISIONS.md, at the top level of a directory
    directly under REPOS_DIR, are ever read. No recursion, no globbing for
    other markdown.
  * The resolved path is checked to still be inside REPOS_DIR after
    resolution, so a symlink inside a repo cannot walk the console out of the
    mount and into, say, /etc.

Markdown is rendered as escaped text with light structure rather than through
a full markdown-to-HTML library: these files are read for their content, and
an extra parser is an extra thing that can be exploited by a file the console
does not control.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.settings import get_settings

logger = logging.getLogger(__name__)

DECISION_RE = re.compile(r"^#{1,4}\s*(D-\d+)\b[:\s-]*(.*)$", re.MULTILINE)


@dataclass
class DecisionDoc:
    repo: str
    path: str
    text: str
    decision_count: int
    modified: float


def _repo_root() -> Path:
    return Path(get_settings().repos_dir).resolve()


def list_repos() -> list[str]:
    root = _repo_root()
    if not root.is_dir():
        logger.warning("repos dir %s is not mounted", root)
        return []
    out = []
    for child in sorted(root.iterdir()):
        try:
            if child.is_dir() and (child / "DECISIONS.md").is_file():
                out.append(child.name)
        except OSError:
            continue
    return out


def read_decisions(repo: str) -> DecisionDoc | None:
    root = _repo_root()
    # Reject anything that is not a plain directory name before touching disk.
    if not repo or "/" in repo or repo.startswith("."):
        return None

    candidate = (root / repo / "DECISIONS.md").resolve()
    # After resolution — this is what defeats a symlink pointing outside.
    if not str(candidate).startswith(str(root) + "/"):
        logger.warning("refused DECISIONS.md path outside repos dir: %s", candidate)
        return None
    if not candidate.is_file():
        return None

    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.exception("could not read %s", candidate)
        return None

    return DecisionDoc(
        repo=repo,
        path=str(candidate),
        text=text,
        decision_count=len(DECISION_RE.findall(text)),
        modified=candidate.stat().st_mtime,
    )


def search(repo_text: str, needle: str) -> list[tuple[int, str]]:
    """Plain substring search, returning (line number, line). Case-insensitive.
    Good enough for a decisions log and has no regex-injection surface."""
    if not needle:
        return []
    low = needle.lower()
    return [
        (i, line)
        for i, line in enumerate(repo_text.splitlines(), start=1)
        if low in line.lower()
    ]
