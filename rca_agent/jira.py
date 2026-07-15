"""Jira via the Atlassian remote MCP server.

The design's choice: the agent fetches tickets through the hosted Atlassian MCP
server (OAuth bridge), rather than us managing a Jira PAT. This module holds the
MCP server config + the read-only tool allow-list the agent may call. Writes
(comments, transitions) are deliberately excluded — the agent stays read-only;
posting a verdict back to Jira routes through human approval later.

No SDK import here, so it's testable and importable from the deterministic path.
"""

from __future__ import annotations

import base64

from .tickets import normalize_jira_issue

# Hosted Atlassian remote MCP server (OAuth handled by the MCP auth flow).
ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"

# Read-only Jira/Atlassian tools the agent is allowed to call.
JIRA_READ_TOOLS = (
    "getJiraIssue",
    "searchJiraIssuesUsingJql",
    "getJiraIssueRemoteIssueLinks",
    "getAccessibleAtlassianResources",
    "atlassianUserInfo",
)


def _extract_pdf_text(pdf_bytes: bytes, max_chars: int = 8000) -> str:
    """Extract plain text from a PDF using pdfplumber. Truncates to max_chars."""
    try:
        import pdfplumber, io
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            parts = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                parts.append(text)
                if sum(len(p) for p in parts) >= max_chars:
                    break
        return "\n".join(parts)[:max_chars]
    except ImportError:
        return "[PDF extraction unavailable — install pdfplumber]"
    except Exception as e:
        return f"[PDF extraction failed: {e}]"


def atlassian_mcp_server_config(url: str = ATLASSIAN_MCP_URL) -> dict:
    """The mcp_servers entry for ClaudeAgentOptions (remote HTTP MCP server)."""
    return {"type": "http", "url": url}


def atlassian_allowed_tools(server_name: str = "atlassian") -> list[str]:
    """Fully-qualified tool names to add to ClaudeAgentOptions.allowed_tools."""
    return [f"mcp__{server_name}__{t}" for t in JIRA_READ_TOOLS]


class JiraError(RuntimeError):
    pass


class JiraClient:
    """Direct Jira Cloud REST client (API v3) — the self-contained fetch path.

    Uses an API token + account email via HTTP Basic auth, so the app fetches
    tickets from plain Python with no MCP / OAuth / Claude-Code-session
    dependency. Descriptions/comments come back as ADF and are flattened by
    `normalize_jira_issue` (so a stack trace in a code block survives).

    Implements the TicketSource protocol (`get(key) -> (key, text)`).
    """

    _FIELDS = "summary,description,status,priority,issuetype,labels,comment,attachment"

    def __init__(self, base_url: str, email: str, token: str, timeout: float = 20.0):
        try:
            import httpx  # lazy: the mock path needs no deps
        except ImportError as e:  # pragma: no cover
            raise JiraError("JiraClient requires httpx (pip install httpx)") from e
        self._base = base_url.rstrip("/") + "/rest/api/3"
        cred = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Basic {cred}", "Accept": "application/json"},
        )

    def close(self) -> None:
        """Release the underlying httpx connection pool (and its sockets)."""
        self._client.close()

    def __enter__(self) -> "JiraClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_issue(self, key: str) -> dict:
        ep = f"{self._base}/issue/{key}"
        r = self._client.get(ep, params={"fields": self._FIELDS})
        if r.status_code == 404:
            raise JiraError(f"issue not found: {key}")
        if r.status_code >= 400:
            raise JiraError(f"Jira {r.status_code} on {key}: {r.text[:200]}")
        return r.json()

    def get(self, key: str, drop_rca_comments: bool = True,
            drop_all_comments: bool = False) -> tuple[str, str]:
        """TicketSource: fetch issue and normalize to (key, ticket_text).

        With drop_all_comments=True, no comments are passed to the agent so it
        investigates independently without being influenced by prior human guesses."""
        return normalize_jira_issue(self.get_issue(key),
                                    drop_rca_comments=drop_rca_comments,
                                    drop_all_comments=drop_all_comments)

    def search(self, jql: str, max_results: int = 50) -> list[dict]:
        ep = f"{self._base}/search/jql"
        r = self._client.get(ep, params={"jql": jql, "maxResults": max_results,
                                          "fields": self._FIELDS})
        if r.status_code >= 400:
            raise JiraError(f"Jira {r.status_code} on search: {r.text[:200]}")
        return r.json().get("issues", [])

    _IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
    _PDF_TYPES   = {"application/pdf"}
    _MAX_BYTES   = 10 * 1024 * 1024  # 10 MB per attachment
    _MAX_IMAGES  = 4
    _MAX_PDFS    = 3
    _MAX_PDF_CHARS = 8000            # truncate extracted PDF text to keep tokens sane

    def get_image_attachments(self, key: str) -> list[dict]:
        """Return up to _MAX_IMAGES image attachments as base64.
        Each entry: {filename, mimeType, content (base64 str)}."""
        return self._get_attachments(key)["images"]

    def get_pdf_texts(self, key: str) -> list[dict]:
        """Return up to _MAX_PDFS PDF attachments as extracted text.
        Each entry: {filename, text}."""
        return self._get_attachments(key)["pdfs"]

    def get_all_attachments(self, key: str) -> dict:
        """Return both images and PDF texts in one call (single issue fetch)."""
        return self._get_attachments(key)

    def _get_attachments(self, key: str) -> dict:
        issue = self.get_issue(key)
        attachments = issue.get("fields", {}).get("attachment") or []
        images, pdfs = [], []
        for att in attachments:
            mime = (att.get("mimeType") or "").lower()
            size = att.get("size", 0)
            url  = att.get("content")
            if not url or size > self._MAX_BYTES:
                continue
            if mime in self._IMAGE_TYPES and len(images) < self._MAX_IMAGES:
                try:
                    r = self._client.get(url, follow_redirects=True)
                    r.raise_for_status()
                    import base64 as _b64
                    import io
                    from PIL import Image
                    img = Image.open(io.BytesIO(r.content))
                    # Legibility win = the RESOLUTION bump (800 -> ~1568px, the model's
                    # effective max) + higher JPEG quality (was q60, which smeared
                    # invoice numbers / GSTINs / error text). We deliberately stay on
                    # compact JPEG, NOT lossless PNG: the image is sent as base64 over
                    # the CLI subprocess stdin, and an oversized blob can stall that
                    # (multimodal) path and hang the run — so keep the payload small.
                    edge = 1568
                    if max(img.width, img.height) > edge:
                        ratio = edge / max(img.width, img.height)
                        img = img.resize((int(img.width * ratio), int(img.height * ratio)),
                                         Image.LANCZOS)
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, format="JPEG", quality=90)
                    images.append({
                        "filename": att.get("filename", "attachment"),
                        "mimeType": "image/jpeg",
                        "content": _b64.b64encode(buf.getvalue()).decode(),
                    })
                except Exception:
                    continue
            elif mime in self._PDF_TYPES and len(pdfs) < self._MAX_PDFS:
                try:
                    r = self._client.get(url)
                    r.raise_for_status()
                    text = _extract_pdf_text(r.content, self._MAX_PDF_CHARS)
                    if text.strip():
                        pdfs.append({
                            "filename": att.get("filename", "attachment.pdf"),
                            "text": text,
                        })
                except Exception:
                    continue
        return {"images": images, "pdfs": pdfs}

    def add_comment(self, key: str, text: str) -> dict:
        """Post a plain-text comment (write-back). NOT called automatically —
        the agent is read-only; route verdict posting through human approval."""
        body = {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}
        return self.add_comment_adf(key, body)

    def add_comment_adf(self, key: str, adf: dict) -> dict:
        ep = f"{self._base}/issue/{key}/comment"
        r = self._client.post(ep, json={"body": adf})
        if r.status_code >= 400:
            raise JiraError(f"Jira {r.status_code} posting comment: {r.text[:200]}")
        return r.json()

    def post_verdict(self, key: str, verdict) -> dict:
        """Post a verdict as a Jira comment — short summary + collapsible 'RCA
        details'. WRITE-BACK: gated; call only after human approval."""
        from .schema import verdict_to_adf
        return self.add_comment_adf(key, verdict_to_adf(verdict))
