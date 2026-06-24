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
    _MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB per image
    _MAX_IMAGES = 4                      # cap to avoid token explosion

    def get_image_attachments(self, key: str) -> list[dict]:
        """Return up to _MAX_IMAGES image attachments for a ticket.

        Each entry: {filename, mimeType, content (base64 str)}.
        Non-image attachments and images over _MAX_IMAGE_BYTES are skipped.
        """
        issue = self.get_issue(key)
        attachments = issue.get("fields", {}).get("attachment") or []
        results = []
        for att in attachments:
            mime = (att.get("mimeType") or "").lower()
            if mime not in self._IMAGE_TYPES:
                continue
            size = att.get("size", 0)
            if size > self._MAX_IMAGE_BYTES:
                continue
            url = att.get("content")
            if not url:
                continue
            try:
                r = self._client.get(url)
                r.raise_for_status()
                import base64 as _b64
                results.append({
                    "filename": att.get("filename", "attachment"),
                    "mimeType": mime,
                    "content": _b64.b64encode(r.content).decode(),
                })
            except Exception:
                continue
            if len(results) >= self._MAX_IMAGES:
                break
        return results

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
