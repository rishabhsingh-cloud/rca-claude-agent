"""The reviewer can choose which branch each repo's AI-fix MR merges into.

`raise_mr` must (a) cut the fix branch from the chosen target, (b) re-apply the
reviewed edits against the CHOSEN target's content (not the integration default),
and (c) set the MR `target_branch` to that choice — falling back to
`client.default_ref` (the integration branch) for any repo the reviewer left alone.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from rca_agent import fix_mr
from rca_agent.gitlab_client import MockGitLabClient

PROJECT = "mastersindia/arap-auth-service"


class _FakeRead:
    """Read-only client stub: records the ref each get_file is asked for."""

    def __init__(self, content: str, default: str = "qa-master"):
        self._content, self._default = content, default
        self.get_file_refs: list[str] = []

    def default_ref(self, project: str) -> str:
        return self._default

    def get_file(self, project: str, ref: str, path: str) -> str:
        self.get_file_refs.append(ref)
        return self._content


def _fix() -> dict:
    return {
        "fixable": True,
        "rationale": "flip the flag",
        "files": [{
            "project": PROJECT, "file": "a.py",
            "edits": [{"before": "x = 1", "after": "x = 2", "applied": True}],
        }],
    }


@pytest.fixture
def gitlab_capture(monkeypatch):
    """Intercept the write-side httpx calls; capture the refs raise_mr uses."""
    cap = {"branch_ref": None, "mr_target": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repository/branches"):
            cap["branch_ref"] = request.url.params.get("ref")
            return httpx.Response(201, json={})
        if path.endswith("/repository/commits"):
            return httpx.Response(201, json={})
        if path.endswith("/merge_requests"):
            cap["mr_target"] = json.loads(request.content)["target_branch"]
            return httpx.Response(201, json={"web_url": "http://mr/1", "iid": 1})
        return httpx.Response(404, json={})

    real_client = httpx.Client

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client)
    monkeypatch.setenv("GITLAB_FIX_TOKEN", "fake-dev-token")
    monkeypatch.setattr(fix_mr, "get_settings",
                        lambda: SimpleNamespace(gitlab_url="https://gitlab.example.com"))
    return cap


def test_chosen_target_drives_branch_ref_apply_ref_and_mr_target(gitlab_capture):
    read = _FakeRead("x = 1\n")
    res = fix_mr.raise_mr("AUT-1", _fix(), read, targets={PROJECT: "master"})

    assert res["results"][0]["mr_url"] == "http://mr/1"
    assert res["results"][0]["target_branch"] == "master"
    assert gitlab_capture["branch_ref"] == "master"   # branch cut from the choice
    assert gitlab_capture["mr_target"] == "master"    # MR merges into the choice
    assert read.get_file_refs == ["master"]           # re-applied against the choice


def test_falls_back_to_integration_default_when_unspecified(gitlab_capture):
    read = _FakeRead("x = 1\n", default="qa-master")
    res = fix_mr.raise_mr("AUT-2", _fix(), read)  # no targets

    assert res["results"][0]["target_branch"] == "qa-master"
    assert gitlab_capture["branch_ref"] == "qa-master"
    assert gitlab_capture["mr_target"] == "qa-master"
    assert read.get_file_refs == ["qa-master"]


def test_blank_target_for_a_repo_still_falls_back(gitlab_capture):
    read = _FakeRead("x = 1\n", default="qa-master")
    res = fix_mr.raise_mr("AUT-3", _fix(), read, targets={PROJECT: "   "})

    assert res["results"][0]["target_branch"] == "qa-master"
    assert gitlab_capture["mr_target"] == "qa-master"


def test_mock_list_branches_falls_back_to_default_without_fixture():
    m = MockGitLabClient()
    # No fixture `branches` meta for an unmapped repo -> just its default_ref.
    got = m.list_branches("acme/thing")
    assert got == [m.default_ref("acme/thing")]
