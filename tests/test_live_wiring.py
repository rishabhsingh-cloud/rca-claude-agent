"""Tests for the live-wiring layer (Jira-via-MCP normalization + GitLab file
enumeration / client-driven indexing). All offline — no MCP, no network."""

from __future__ import annotations

from rca_agent.config import get_settings
from rca_agent.gitlab_client import MockGitLabClient
from rca_agent.graph_store import build_repo_graph, read_sources_via_client
from rca_agent.investigation import investigate
from rca_agent.jira import atlassian_allowed_tools, atlassian_mcp_server_config
from rca_agent.stack_trace import crash_site, parse_stack_trace
from rca_agent.tickets import (
    MockTicketSource,
    flatten_adf,
    load_fixture_ticket,
    normalize_jira_issue,
)

PROJECT = "acme/billing-service"


# --- Jira ADF normalization ----------------------------------------------------

def test_flatten_adf_preserves_code_block_newlines():
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "see log:"}]},
            {"type": "codeBlock", "content": [{"type": "text", "text": "line1\nline2"}]},
        ],
    }
    out = flatten_adf(doc)
    assert "see log:" in out
    assert "line1\nline2" in out  # code-block newlines survive


def test_normalize_jira_issue_extracts_traceback_from_adf():
    key, text = load_fixture_ticket("RCA-201")  # raw Atlassian shape -> normalized
    assert key == "RCA-201"
    # The trace, buried in an ADF code block, must survive into the text.
    frames = parse_stack_trace(text)
    assert crash_site(frames).ref() == "billing/invoice.py:27"


def test_normalize_handles_plain_string_description():
    issue = {"key": "X-1", "fields": {"summary": "s", "description": "plain text body"}}
    key, text = normalize_jira_issue(issue)
    assert key == "X-1" and "plain text body" in text


def test_normalize_jira_response_unwraps_mcp_envelope():
    from rca_agent.tickets import normalize_jira_response
    # Shape returned by the Atlassian MCP getJiraIssue tool.
    resp = {"issues": {"nodes": [{
        "key": "AUT-1",
        "fields": {
            "summary": "Accept fails",
            "status": {"name": "Open"},
            "description": {"type": "doc", "content": [
                {"type": "codeBlock", "content": [{"type": "text",
                 "text": 'File "billing/invoice.py", line 27, in compute_total'}]}]},
        },
    }]}}
    key, text = normalize_jira_response(resp)
    assert key == "AUT-1"
    assert "billing/invoice.py" in text  # trace survived ADF + envelope unwrap


# --- Jira REST client (direct fetch path) -------------------------------------

def test_jira_client_get_normalizes(monkeypatch):
    from rca_agent.jira import JiraClient
    c = JiraClient("https://x.atlassian.net", "e@x.com", "tok")
    issue = {"key": "AUT-9", "fields": {
        "summary": "Pending liability not reflecting",
        "status": {"name": "Open"},
        "description": {"type": "doc", "content": [
            {"type": "codeBlock", "content": [{"type": "text",
             "text": 'File "billing/invoice.py", line 27, in compute_total'}]}]},
    }}
    monkeypatch.setattr(c, "get_issue", lambda key: issue)
    key, text = c.get("AUT-9")
    assert key == "AUT-9" and "billing/invoice.py" in text


def test_build_ticket_source_mock_vs_jira():
    from rca_agent.config import Settings
    from rca_agent.tickets import MockTicketSource, build_ticket_source

    no_jira = Settings("mock", None, None, None, None, None, "m", 12)
    assert isinstance(build_ticket_source(no_jira), MockTicketSource)

    with_jira = Settings("live", None, None, "https://x.atlassian.net",
                         "e@x.com", "tok", "m", 12)
    src = build_ticket_source(with_jira)
    assert type(src).__name__ == "JiraClient"


def test_mock_ticket_source():
    assert MockTicketSource().get("RCA-201")[0] == "RCA-201"


# --- Atlassian MCP wiring config ----------------------------------------------

def test_atlassian_mcp_config_shape():
    cfg = atlassian_mcp_server_config()
    assert cfg["type"] == "http" and cfg["url"].startswith("https://")
    tools = atlassian_allowed_tools("atlassian")
    assert "mcp__atlassian__getJiraIssue" in tools
    # read-only: no write tools leak in
    assert not any("create" in t or "edit" in t or "addComment" in t for t in tools)


# --- GitLab file enumeration / client-driven indexing -------------------------

def test_client_enumerates_and_reads_sources():
    c = MockGitLabClient()
    files = c.list_source_files(PROJECT, "main")
    assert files == ["billing/api.py", "billing/invoice.py"]
    assert "def compute_total" in c.get_file(PROJECT, "main", "billing/invoice.py")


def test_indexer_via_client_matches_disk_build():
    # Building through the GitLabClient (the live path) yields the same graph as
    # the disk fixture build (the AST is the same; only the source plumbing differs).
    via_client = build_repo_graph(PROJECT, client=MockGitLabClient())
    via_disk = build_repo_graph(PROJECT)
    assert set(via_client.nodes) == set(via_disk.nodes)
    assert via_client.has_edge("compute_total", "subtotal")


def test_read_sources_via_client():
    srcs = read_sources_via_client(MockGitLabClient(), PROJECT, "main")
    assert set(srcs) == {"billing/api.py", "billing/invoice.py"}


# --- end-to-end on the Jira ADF ticket ----------------------------------------

def test_investigate_on_jira_adf_ticket():
    key, text = load_fixture_ticket("RCA-201")
    v = investigate(key, text, MockGitLabClient(), get_settings())
    assert v.confidence.value == "high"
    assert v.is_regression is True
    assert any("create_invoice_endpoint" in b for b in v.blast_radius)
