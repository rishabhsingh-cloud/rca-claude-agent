"""GitLab access behind one interface, two implementations.

  MockGitLabClient — reads recorded fixtures (no network / no PAT). Default.
  RestGitLabClient — read-only calls against a self-hosted GitLab REST API.

Everything the trace-first slice needs maps to a deterministic GitLab artifact:
  get_file_lines     -> repository file content (the suspect line in context)
  blame_line         -> the exact commit that last changed a line  (regression!)
  get_commit         -> commit metadata for the evidence chain
  merge_requests_for_commit -> the MR that introduced the change

The client is strictly read-only; nothing here writes to repos.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from .config import FIXTURES_DIR


# --- Value types ---------------------------------------------------------------

@dataclass(frozen=True)
class FileSlice:
    path: str
    ref: str
    start_line: int
    end_line: int
    # (line_number, text) pairs, 1-based line numbers.
    numbered_lines: list[tuple[int, str]]

    def render(self) -> str:
        width = len(str(self.end_line))
        return "\n".join(f"{n:>{width}}  {t}" for n, t in self.numbered_lines)


@dataclass(frozen=True)
class Commit:
    id: str
    short_id: str
    title: str
    author_name: str
    authored_date: str  # ISO-8601
    message: str


@dataclass(frozen=True)
class MergeRequest:
    iid: int
    title: str
    author: str
    web_url: str
    merged_at: str | None
    state: str


class GitLabError(RuntimeError):
    pass


# --- Web URL helpers (clickable links for the QA-navigable verdict) ------------

def web_blob_url(base_url: str, project: str, ref: str, path: str,
                 line: int | None = None) -> str:
    """Clickable GitLab link to a file (optionally at a line)."""
    if not base_url:
        return ""
    url = f"{base_url.rstrip('/')}/{project}/-/blob/{ref}/{path}"
    return url + (f"#L{line}" if line else "")


def web_commit_url(base_url: str, project: str, sha: str) -> str:
    if not base_url:
        return ""
    return f"{base_url.rstrip('/')}/{project}/-/commit/{sha}"


SOURCE_EXTS = (".py",)  # AST graph is Python-only; widen when more engines land.


@runtime_checkable
class GitLabClient(Protocol):
    def get_file_lines(self, project: str, ref: str, path: str,
                       start: int, end: int) -> FileSlice: ...

    def get_file(self, project: str, ref: str, path: str) -> str: ...

    def default_ref(self, project: str) -> str: ...

    def list_source_files(self, project: str, ref: str,
                          exts: tuple[str, ...] = SOURCE_EXTS) -> list[str]: ...

    def search_blobs(self, project: str, ref: str, query: str,
                     limit: int = 20) -> list[dict]: ...

    def blame_line(self, project: str, ref: str, path: str, line: int) -> Commit | None: ...

    def get_commit(self, project: str, sha: str) -> Commit | None: ...

    def merge_requests_for_commit(self, project: str, sha: str) -> list[MergeRequest]: ...


# --- Mock implementation -------------------------------------------------------

class MockGitLabClient:
    """Backed by on-disk fixtures under fixtures/gitlab/<project>/.

    Layout (see fixtures/gitlab/acme__billing-service/):
      <project>/files/<path>       real source files, byte-for-byte
      <project>/meta.json          {
          "ref": "main",
          "blame":  { "<path>": [ {"commit": "<sha>", "count": <int>}, ... ] },
          "commits": { "<sha>": { id, short_id, title, author_name, authored_date, message } },
          "commit_mrs": { "<sha>": [ { iid, title, author, web_url, merged_at, state } ] }
        }
    The blame list is in file order; each span owns `count` consecutive lines,
    so line N maps to whichever span contains it. Storing source as real files
    (not escaped JSON) keeps fixtures readable and avoids line-ending drift.
    """

    def __init__(self, fixtures_dir: Path | None = None):
        self._dir = (fixtures_dir or FIXTURES_DIR) / "gitlab"
        self._meta_cache: dict[str, dict] = {}

    def _project_dir(self, project: str) -> Path:
        return self._dir / project.replace("/", "__")

    def _meta(self, project: str) -> dict:
        if project not in self._meta_cache:
            fp = self._project_dir(project) / "meta.json"
            if not fp.exists():
                raise GitLabError(f"no fixture for project '{project}' (looked for {fp})")
            self._meta_cache[project] = json.loads(fp.read_text(encoding="utf-8"))
        return self._meta_cache[project]

    def get_file_lines(self, project: str, ref: str, path: str,
                       start: int, end: int) -> FileSlice:
        fp = self._project_dir(project) / "files" / path
        if not fp.exists():
            raise GitLabError(f"file not found in fixture: {path}")
        lines = fp.read_text(encoding="utf-8").splitlines()
        start = max(1, start)
        end = min(len(lines), end) if end else len(lines)
        numbered = [(i, lines[i - 1]) for i in range(start, end + 1)]
        return FileSlice(path=path, ref=ref, start_line=start, end_line=end,
                         numbered_lines=numbered)

    def get_file(self, project: str, ref: str, path: str) -> str:
        fp = self._project_dir(project) / "files" / path
        if not fp.exists():
            raise GitLabError(f"file not found in fixture: {path}")
        return fp.read_text(encoding="utf-8")

    def default_ref(self, project: str) -> str:
        try:
            return self._meta(project).get("ref") or "main"
        except GitLabError:
            return "main"

    def list_source_files(self, project: str, ref: str,
                          exts: tuple[str, ...] = SOURCE_EXTS) -> list[str]:
        base = self._project_dir(project) / "files"
        if not base.exists():
            return []
        out = [fp.relative_to(base).as_posix()
               for fp in base.rglob("*") if fp.suffix in exts]
        return sorted(out)

    def search_blobs(self, project: str, ref: str, query: str,
                     limit: int = 20) -> list[dict]:
        base = self._project_dir(project) / "files"
        q = query.lower()
        out: list[dict] = []
        if not base.exists():
            return out
        for fp in sorted(base.rglob("*")):
            if not fp.is_file():
                continue
            try:
                lines = fp.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            rel = fp.relative_to(base).as_posix()
            for i, t in enumerate(lines, 1):
                if q in t.lower():
                    out.append({"path": rel, "startline": i, "data": t.strip()})
                    if len(out) >= limit:
                        return out
        return out

    def blame_line(self, project: str, ref: str, path: str, line: int) -> Commit | None:
        spans = self._meta(project).get("blame", {}).get(path)
        if not spans:
            return None
        cursor = 1
        for span in spans:
            n = span.get("count") or len(span.get("lines", []))
            if cursor <= line < cursor + n:
                return self.get_commit(project, span["commit"])
            cursor += n
        return None

    def get_commit(self, project: str, sha: str) -> Commit | None:
        c = self._meta(project).get("commits", {}).get(sha)
        if not c:
            return None
        return Commit(
            id=c["id"], short_id=c.get("short_id", c["id"][:8]),
            title=c["title"], author_name=c["author_name"],
            authored_date=c["authored_date"], message=c.get("message", c["title"]),
        )

    def merge_requests_for_commit(self, project: str, sha: str) -> list[MergeRequest]:
        out = []
        for m in self._meta(project).get("commit_mrs", {}).get(sha, []):
            out.append(MergeRequest(
                iid=m["iid"], title=m["title"], author=m["author"],
                web_url=m["web_url"], merged_at=m.get("merged_at"),
                state=m.get("state", "merged"),
            ))
        return out


# --- Live read-only REST implementation ---------------------------------------

class RestGitLabClient:
    """Read-only client for a self-hosted GitLab instance.

    Uses only GET endpoints; the PAT needs `read_api`/`read_repository` scope.
    `project` is the URL-encoded path (e.g. "acme/billing-service") or numeric ID.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 20.0):
        try:
            import httpx  # imported lazily so the mock path needs no deps
        except ImportError as e:  # pragma: no cover
            raise GitLabError("RestGitLabClient requires httpx (pip install httpx)") from e
        self._httpx = httpx
        self._base = base_url.rstrip("/") + "/api/v4"
        self._client = httpx.Client(
            timeout=timeout,
            headers={"PRIVATE-TOKEN": token},
        )
        self._default_ref_cache: dict[str, str] = {}

    def _pid(self, project: str) -> str:
        return quote(project, safe="") if "/" in project else project

    def default_ref(self, project: str) -> str:
        """The repo's default branch (cached) — so callers/agent don't hardcode
        'main' for repos that use qa-master / pre-pro-master / etc."""
        if project not in self._default_ref_cache:
            info = self._get(f"/projects/{self._pid(project)}")
            self._default_ref_cache[project] = (info or {}).get("default_branch") or "main"
        return self._default_ref_cache[project]

    def _get(self, path: str, **params):
        r = self._client.get(self._base + path, params=params)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise GitLabError(f"GitLab {r.status_code} on {path}: {r.text[:200]}")
        return r.json()

    def get_file_lines(self, project: str, ref: str, path: str,
                       start: int, end: int) -> FileSlice:
        # /repository/files/:file_path/raw?ref=  -> plain text body
        ep = f"/projects/{self._pid(project)}/repository/files/{quote(path, safe='')}/raw"
        r = self._client.get(self._base + ep, params={"ref": ref})
        if r.status_code == 404:
            raise GitLabError(f"file not found: {path}@{ref}")
        if r.status_code >= 400:
            raise GitLabError(f"GitLab {r.status_code} fetching {path}: {r.text[:200]}")
        lines = r.text.splitlines()
        start = max(1, start)
        end = min(len(lines), end)
        numbered = [(i, lines[i - 1]) for i in range(start, end + 1)]
        return FileSlice(path=path, ref=ref, start_line=start, end_line=end,
                         numbered_lines=numbered)

    def get_file(self, project: str, ref: str, path: str) -> str:
        ep = f"/projects/{self._pid(project)}/repository/files/{quote(path, safe='')}/raw"
        r = self._client.get(self._base + ep, params={"ref": ref})
        if r.status_code == 404:
            raise GitLabError(f"file not found: {path}@{ref}")
        if r.status_code >= 400:
            raise GitLabError(f"GitLab {r.status_code} fetching {path}: {r.text[:200]}")
        return r.text

    def list_source_files(self, project: str, ref: str,
                          exts: tuple[str, ...] = SOURCE_EXTS) -> list[str]:
        # /repository/tree?recursive=true&ref=  -> [{type, path, ...}], paginated.
        ep = f"/projects/{self._pid(project)}/repository/tree"
        out: list[str] = []
        page = 1
        while True:
            r = self._client.get(self._base + ep, params={
                "ref": ref, "recursive": "true", "per_page": 100, "page": page})
            if r.status_code >= 400:
                raise GitLabError(f"GitLab {r.status_code} listing tree: {r.text[:200]}")
            rows = r.json()
            if not rows:
                break
            out.extend(e["path"] for e in rows
                       if e.get("type") == "blob" and any(e["path"].endswith(x) for x in exts))
            nxt = r.headers.get("x-next-page")
            if not nxt:
                break
            page = int(nxt)
        return sorted(out)

    def search_blobs(self, project: str, ref: str, query: str,
                     limit: int = 20) -> list[dict]:
        # /projects/:id/search?scope=blobs  -> [{path, startline, data, ...}]
        ep = f"/projects/{self._pid(project)}/search"
        rows = self._get(ep, scope="blobs", search=query, ref=ref) or []
        return [{"path": r.get("path"), "startline": r.get("startline"),
                 "data": (r.get("data") or "").strip()[:200]} for r in rows[:limit]]

    def blame_line(self, project: str, ref: str, path: str, line: int) -> Commit | None:
        # /repository/files/:file_path/blame?ref=  -> [{commit:{...}, lines:[...]}]
        ep = f"/projects/{self._pid(project)}/repository/files/{quote(path, safe='')}/blame"
        spans = self._get(ep, ref=ref)
        if not spans:
            return None
        cursor = 1
        for span in spans:
            n = len(span.get("lines", []))
            if cursor <= line < cursor + n:
                c = span["commit"]
                commit = Commit(
                    id=c["id"], short_id=c.get("short_id", c["id"][:8]),
                    title=c.get("title", ""), author_name=c.get("author_name", ""),
                    authored_date=c.get("authored_date", ""),
                    message=c.get("message", c.get("title", "")),
                )
                # Some GitLab versions omit `title`/metadata from blame's commit
                # object (observed on 13.12) — enrich from the commit endpoint.
                if not commit.title:
                    full = self.get_commit(project, commit.id)
                    if full:
                        commit = full
                return commit
            cursor += n
        return None

    def get_commit(self, project: str, sha: str) -> Commit | None:
        ep = f"/projects/{self._pid(project)}/repository/commits/{sha}"
        c = self._get(ep)
        if not c:
            return None
        return Commit(
            id=c["id"], short_id=c.get("short_id", c["id"][:8]),
            title=c.get("title", ""), author_name=c.get("author_name", ""),
            authored_date=c.get("authored_date", ""),
            message=c.get("message", c.get("title", "")),
        )

    def merge_requests_for_commit(self, project: str, sha: str) -> list[MergeRequest]:
        ep = f"/projects/{self._pid(project)}/repository/commits/{sha}/merge_requests"
        rows = self._get(ep) or []
        out = []
        for m in rows:
            out.append(MergeRequest(
                iid=m["iid"], title=m.get("title", ""),
                author=(m.get("author") or {}).get("username", ""),
                web_url=m.get("web_url", ""), merged_at=m.get("merged_at"),
                state=m.get("state", ""),
            ))
        return out


# --- Factory -------------------------------------------------------------------

def build_client(settings) -> GitLabClient:
    if settings.backend == "live":
        if not settings.gitlab_url or not settings.gitlab_token:
            raise GitLabError("live backend needs GITLAB_URL and GITLAB_TOKEN")
        return RestGitLabClient(settings.gitlab_url, settings.gitlab_token)
    return MockGitLabClient()
