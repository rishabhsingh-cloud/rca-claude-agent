"""Configuration + backend selection.

Two backends for the GitLab client:
  - "mock": reads recorded fixtures (no network, no credentials) — default.
  - "live": hits a self-hosted GitLab REST API with a read-scope PAT.

Selection is via env so the same tools/agent code runs against either.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Repo root (……/rca-agent). Fixtures live under <root>/fixtures.
ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "fixtures"


def index_dir() -> Path:
    """Where published indexes (graph maps + summaries) live.

    Defaults to the fixtures dir (so mock tests find the mock index). Set
    RCA_INDEX_DIR for a real deployment so the live index of real repos is kept
    separate from test fixtures — the design's "dedicated index store".
    """
    return Path(os.environ.get("RCA_INDEX_DIR") or FIXTURES_DIR)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency). Real environment variables win —
    we only fill in keys not already set."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    # "mock" | "live"
    backend: str
    # Live GitLab only:
    gitlab_url: str | None
    gitlab_token: str | None
    # Live Jira (Cloud REST) only:
    jira_url: str | None
    jira_email: str | None
    jira_token: str | None
    # Model used by the agent loop (Agent SDK). Deterministic core ignores this.
    model: str
    # How many lines of context to fetch around a suspect line.
    context_lines: int

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            backend=os.getenv("RCA_BACKEND", "mock").strip().lower(),
            gitlab_url=os.getenv("GITLAB_URL"),
            gitlab_token=os.getenv("GITLAB_TOKEN"),
            jira_url=os.getenv("JIRA_URL") or os.getenv("JIRA_BASE_URL"),
            jira_email=os.getenv("JIRA_EMAIL"),
            jira_token=os.getenv("JIRA_TOKEN") or os.getenv("JIRA_API_TOKEN"),
            model=os.getenv("RCA_MODEL", "claude-opus-4-8"),
            context_lines=int(os.getenv("RCA_CONTEXT_LINES", "12")),
        )

    @property
    def has_jira(self) -> bool:
        return bool(self.jira_url and self.jira_email and self.jira_token)


def get_settings() -> Settings:
    return Settings.from_env()
